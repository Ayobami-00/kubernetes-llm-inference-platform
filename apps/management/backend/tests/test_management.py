import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from platform_contracts import Run, WorkerGroup
from platform_management.api.app import app
from platform_management.db.models import Job, Resource, SimInstance
from platform_management.db.seed import seed
from platform_management.db.session import engine, session
from platform_management.jobs.queue import claim
from platform_management.jobs.reconciliation import reconcile_expiry, refresh_capacity
from platform_management.jobs.worker import execute, tick
from platform_management.providers.lambda_cloud import LambdaCloud
from platform_management.services.common import now
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

ROOT = Path(__file__).resolve().parents[4]


def post(client, path, body=None, key=None):
    return client.post("/api/v1" + path, json=body, headers={"Idempotency-Key": key or uuid4().hex})


def settle(limit=40):
    for _ in range(limit):
        with session() as db, db.begin():
            db.execute(update(Job).where(Job.state == "pending").values(due_at=now()))
        if not tick():
            return
    raise AssertionError("Jobs did not settle within bounded test budget")


def create(client, identity="demo-group", fault="none", spec="latency-workers@1"):
    result = post(client, "/worker-groups", {"id": identity, "spec_ref": spec, "fault": fault})
    assert result.status_code == 202, result.text
    return result.json()


def group(identity="demo-group"):
    with session() as db:
        return copy.deepcopy(db.get(Resource, identity).payload)


def test_catalog_seed_and_shared_accounting(client):
    for endpoint, count in [
        ("customers", 5),
        ("scenarios", 7),
        ("worker-group-specs", 3),
        ("serving-profiles", 4),
        ("datasets", 6),
    ]:
        response = client.get("/api/v1/" + endpoint)
        assert response.status_code == 200
        assert len(response.json()) == count
    with session() as db, db.begin():
        assert seed(db, ROOT) == 25
    graph = client.get("/api/v1/topology").json()
    assert graph["counts"]["planned_gpus"] == 13
    assert len([n for n in graph["nodes"] if n["id"] == "deployment:shared-medium"]) == 1
    assert client.get("/api/v1/configuration/export").headers["content-type"] == "application/zip"


def test_authentication_csrf_logout_and_throttle(client):
    with TestClient(app, base_url="http://localhost") as anonymous:
        assert anonymous.get("/api/v1/customers").status_code == 401
        for _ in range(5):
            assert (
                anonymous.post(
                    "/api/v1/auth/login", json={"username": "admin", "password": "wrong"}
                ).status_code
                == 401
            )
        assert (
            anonymous.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "wrong"}
            ).status_code
            == 429
        )
    token = client.headers.pop("X-CSRF-Token")
    assert (
        post(
            client, "/worker-groups", {"id": "blocked", "spec_ref": "latency-workers@1"}
        ).status_code
        == 403
    )
    client.headers["X-CSRF-Token"] = token
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.get("/api/v1/customers").status_code == 401


def test_revisions_preserve_snapshots_and_detect_conflicts(client):
    before = client.get("/api/v1/runs").json()[0]["run"]["snapshot"]
    original = client.get("/api/v1/customers").json()[0]
    document = copy.deepcopy(original["document"])
    document["name"] = "Updated customer"
    saved = client.put(
        f"/api/v1/customers/{document['id']}/draft",
        json={"document": document, "expected_version": original["version"]},
    )
    assert saved.status_code == 200, saved.text
    assert (
        client.put(
            f"/api/v1/customers/{document['id']}/draft",
            json={"document": document, "expected_version": original["version"]},
        ).status_code
        == 409
    )
    published = post(
        client,
        f"/customers/{document['id']}/publish",
        {"expected_version": saved.json()["version"]},
    )
    assert published.status_code == 200, published.text
    assert published.json()["current_revision"] == 2
    assert client.get("/api/v1/runs").json()[0]["run"]["snapshot"] == before
    with pytest.raises(DBAPIError), engine().begin() as connection:
        connection.execute(text("UPDATE revisions SET document='{}'::jsonb"))
    with pytest.raises(DBAPIError), engine().begin() as connection:
        connection.execute(text("UPDATE runs SET snapshot='{}'::jsonb"))


def test_group_create_renew_delete_and_idempotency(client):
    key = uuid4().hex
    body = {"id": "demo-group", "spec_ref": "latency-workers@1"}
    response = post(client, "/worker-groups", body, key)
    assert response.status_code == 202, response.text
    assert post(client, "/worker-groups", body, key).json() == response.json()
    assert post(client, "/worker-groups", {**body, "id": "other"}, key).status_code == 409
    settle()
    ready = group()
    WorkerGroup.model_validate(ready["group"])
    assert ready["group"]["status"]["phase"] == "ready"
    first = ready["group"]["status"]["allocated_at"]
    expiry = ready["group"]["status"]["expires_at"]
    assert (
        post(client, "/worker-groups/demo-group/renew", {"additional_minutes": 30}).status_code
        == 202
    )
    settle()
    renewed = group()["group"]["status"]
    assert renewed["allocated_at"] == first
    assert renewed["expires_at"] > expiry
    assert post(client, "/worker-groups/demo-group/delete").status_code == 202
    settle()
    assert group()["group"]["status"]["phase"] == "deleted"
    operation = client.get("/api/v1/operations/" + response.json()["operation_id"]).json()[
        "operation"
    ]
    assert operation["status"] == "completed"


@pytest.mark.parametrize(
    "fault,expected",
    [("shortage", "failed"), ("transient", "ready"), ("ambiguous", "ready"), ("partial", "failed")],
)
def test_provider_failures_reconcile_and_cleanup(client, fault, expected):
    result = create(client, fault=fault)
    settle()
    current = group()["group"]
    assert current["status"]["phase"] == expected
    op = client.get("/api/v1/operations/" + result["operation_id"]).json()["operation"]
    assert op["status"] == ("completed" if expected == "ready" else "failed")
    assert not op["cleanup_pending"]
    with session() as db:
        instances = list(db.scalars(select(SimInstance)))
        assert len(instances) < 2
        if fault == "partial":
            assert instances and all(i.state == "terminated" for i in instances)


def test_termination_failure_remains_pending_then_manual_cleanup(client):
    create(client, fault="termination")
    settle()
    result = post(client, "/worker-groups/demo-group/delete").json()
    settle()
    op = client.get("/api/v1/operations/" + result["operation_id"]).json()["operation"]
    assert op["status"] == "failed" and op["cleanup_pending"]
    assert group()["group"]["status"]["phase"] == "draining"
    assert client.put("/api/v1/worker-groups/demo-group/simulation?fault=none").status_code == 200
    assert post(client, "/worker-groups/demo-group/delete").status_code == 202
    settle()
    assert group()["group"]["status"]["phase"] == "deleted"


def test_expiry_requires_confirmed_termination(client):
    create(client)
    settle()
    with session() as db, db.begin():
        row = db.get(Resource, "demo-group")
        value = copy.deepcopy(row.payload)
        value["group"]["status"]["allocated_at"] = (now() - timedelta(minutes=91)).isoformat()
        value["group"]["status"]["expires_at"] = (now() - timedelta(minutes=1)).isoformat()
        row.payload = value
    reconcile_expiry()
    settle()
    assert group()["group"]["status"]["phase"] == "expired"


def test_concurrent_claims_and_restart_recovery(client):
    result = create(client)

    def take(owner):
        with session() as db, db.begin():
            return claim(db, owner)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(take, ["first", "second"]))
    assert sum(v is not None for v in claimed) == 1
    with session() as db, db.begin():
        job = db.get(Job, result["operation_id"])
        job.lease_until = now() - timedelta(seconds=1)
    settle()
    assert group()["group"]["status"]["phase"] == "ready"
    with session() as db:
        assert len(list(db.scalars(select(SimInstance)))) == 1


def test_execution_guard_does_not_allow_expired_owner_and_new_worker_overlap(client):
    result = create(client)
    with session() as db, db.begin():
        claim(db, "old")
    from platform_management.services.common import lock_key

    key = lock_key("execution:demo-group")
    with engine().connect().execution_options(isolation_level="AUTOCOMMIT") as guard:
        guard.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        with session() as db, db.begin():
            db.get(Job, result["operation_id"]).lease_until = now() - timedelta(seconds=1)
        with session() as db, db.begin():
            claim(db, "new")
        execute(result["operation_id"], "new")
        assert not group()["group"]["status"]["members"]
        guard.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
    settle()
    assert group()["group"]["status"]["phase"] == "ready"


def ready_serving(client):
    create(client)
    settle()
    result = post(
        client,
        "/serving-deployments",
        {"id": "gec", "profile_ref": "gec-baseline@1", "group_id": "demo-group"},
    )
    assert result.status_code == 202, result.text
    settle()


@pytest.mark.parametrize(
    "outcome,verdict",
    [
        ("passing", "passing"),
        ("violating", "violating"),
        ("insufficient_data", "insufficient_data"),
        ("invalid", "not_evaluated"),
        ("failed", None),
    ],
)
def test_simulated_run_results_and_frozen_graph(client, outcome, verdict):
    ready_serving(client)
    body = {
        "scenario_ref": "writer-assist-baseline@1",
        "group_ids": {"latency-workers@1": "demo-group"},
        "outcome": outcome,
    }
    response = post(client, "/runs", body)
    assert response.status_code == 202, response.text
    settle()
    value = client.get("/api/v1/runs/" + response.json()["resource_id"]).json()
    run = Run.model_validate(value["run"])
    assert run.fixture and value["mode"] == "simulation"
    if verdict:
        assert run.status == "completed" and run.result.slo_verdict == verdict
        assert all(q.status == "not_evaluated" for q in run.result.quality)
    else:
        assert run.status == "failed"
    graph = client.get(f"/api/v1/runs/{run.id}/topology").json()
    assert graph["historical"]
    assert all(n["provenance"] == "historical" for n in graph["nodes"])


def test_real_execution_remains_blocked_and_run_cancellation(client):
    report = post(client, "/runs/preflight", {"scenario_ref": "writer-assist-baseline@1"}).json()
    assert report["contract_blockers"] and report["runtime_blockers"] and not report["executable"]
    assert (
        post(
            client, "/runs", {"scenario_ref": "writer-assist-baseline@1", "mode": "real"}
        ).status_code
        == 422
    )
    ready_serving(client)
    result = post(
        client,
        "/runs",
        {
            "scenario_ref": "writer-assist-baseline@1",
            "group_ids": {"latency-workers@1": "demo-group"},
        },
    ).json()
    assert post(client, f"/runs/{result['resource_id']}/stop").status_code == 202
    settle()
    assert (
        client.get("/api/v1/runs/" + result["resource_id"]).json()["run"]["status"] == "cancelled"
    )


def test_lambda_read_only_allowlist_pagination_and_unconfigured(client):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("instance-types"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "gpu": {
                            "instance_type": {"name": "gpu", "specs": {"gpus": 1}},
                            "regions_with_capacity_available": [],
                        }
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [{"id": "actual", "status": "active", "jupyter_url": "SENSITIVE"}],
                "page_token": None,
            },
        )

    values = LambdaCloud(
        "test-key", transport=httpx.MockTransport(handler), pause=lambda _: None
    ).observe()
    assert len(calls) == 2 and "jupyter_url" not in json.dumps(values)
    refresh_capacity()
    assert client.get("/api/v1/capacity").json()["status"] == "unconfigured"


def test_generated_openapi_matches_real_routes():
    assert json.loads((ROOT / "contracts/management.openapi.json").read_text()) == app.openapi()


def test_unresolved_allocation_never_repeats_launch_or_completes_cleanup(client, monkeypatch):
    from platform_management.db.models import ProviderIntent
    from platform_management.providers.simulated import SimulatedProvider

    result = create(client)
    calls = []
    monkeypatch.setattr(SimulatedProvider, "allocate", lambda *_: calls.append("allocate"))
    with session() as db, db.begin():
        db.add(
            ProviderIntent(
                id=result["operation_id"],
                group_id="demo-group",
                state="unknown",
                payload={"desired": 1, "fault": "none"},
            )
        )
    settle()
    assert calls == []
    operation = client.get("/api/v1/operations/" + result["operation_id"]).json()["operation"]
    assert operation["status"] == "failed" and operation["cleanup_pending"]
    assert group()["group"]["status"]["phase"] == "draining"
    deletion = post(client, "/worker-groups/demo-group/delete").json()
    settle()
    operation = client.get("/api/v1/operations/" + deletion["operation_id"]).json()["operation"]
    assert operation["status"] == "failed" and operation["cleanup_pending"]
    assert group()["group"]["status"]["phase"] == "draining"


def test_stale_owner_cannot_commit_a_checkpoint(client):
    from platform_management.services.common import AppError

    result = create(client)
    with session() as db, db.begin():
        claim(db, "old-owner")
        db.get(Job, result["operation_id"]).lease_until = now() - timedelta(seconds=1)
    with session() as db, db.begin():
        assert claim(db, "new-owner") == result["operation_id"]
    with pytest.raises(AppError):
        execute(result["operation_id"], "old-owner")
    assert group()["group"]["status"]["members"] == []
    execute(result["operation_id"], "new-owner")
    settle()
    assert group()["group"]["status"]["phase"] == "ready"


def test_expiry_during_preparation_preserves_deadline_and_cleans_up(client):
    result = create(client)
    assert tick() == result["operation_id"]  # allocation confirmed, bootstrap not started
    with session() as db, db.begin():
        row = db.get(Resource, "demo-group")
        value = copy.deepcopy(row.payload)
        first = (now() - timedelta(minutes=91)).isoformat()
        deadline = (now() - timedelta(minutes=1)).isoformat()
        value["group"]["status"].update(allocated_at=first, expires_at=deadline)
        row.payload = value
    settle()
    value = group()["group"]["status"]
    assert value["allocated_at"] == first and value["expires_at"] == deadline
    assert value["phase"] == "expired" and all(m["phase"] == "terminated" for m in value["members"])
    operation = client.get("/api/v1/operations/" + result["operation_id"]).json()["operation"]
    assert operation["status"] == "failed" and not operation["cleanup_pending"]


def test_saved_unpublished_drafts_are_visible_after_reload(client):
    original = client.get("/api/v1/customers").json()[0]["document"]
    original = {**original, "id": "draft-customer", "name": "Draft customer"}
    response = client.put(
        "/api/v1/customers/draft-customer/draft", json={"document": original, "expected_version": 0}
    )
    assert response.status_code == 200, response.text
    graph = client.get("/api/v1/topology").json()
    draft = next(n for n in graph["nodes"] if n["resource_id"] == "draft-customer")
    assert draft["status"] == "draft" and draft["document"]["draft"]["name"] == "Draft customer"


def test_session_expiry_and_layout_preferences(client):
    from platform_management.db.models import SessionRow

    body = {
        "positions": {"customer:writer-assist@1": {"x": 34, "y": 56}},
        "viewport": {"x": 0, "y": 0, "zoom": 0.8},
    }
    assert client.put("/api/v1/preferences", json=body).status_code == 200
    assert client.get("/api/v1/preferences").json() == body
    with session() as db, db.begin():
        db.execute(update(SessionRow).values(expires_at=now() - timedelta(seconds=1)))
    assert client.get("/api/v1/auth/session").status_code == 401


@pytest.mark.parametrize("failure", ["unauthorized", "rate_limit", "timeout"])
def test_capacity_refresh_failure_retains_last_good_observation(
    client, monkeypatch, tmp_path, failure
):
    from platform_management.db.models import Observation
    from platform_management.jobs import reconciliation
    from platform_management.settings import get_settings

    stamp = now().isoformat()
    previous = {
        "source": "lambda",
        "status": "fresh",
        "observed_at": stamp,
        "instance_types": [],
        "instances": [{"id": "observed-instance", "status": "active"}],
    }
    with session() as db, db.begin():
        db.add(Observation(id="lambda", payload=previous, next_refresh=now()))
    credential = tmp_path / "provider-key"
    credential.write_text("test-only-key")
    monkeypatch.setattr(get_settings(), "lambda_api_key_file", credential)

    class FailingProvider:
        def observe(self):
            request = httpx.Request("GET", "https://cloud.lambda.ai/api/v1/instances")
            if failure == "timeout":
                raise httpx.ReadTimeout("timeout", request=request)
            response = httpx.Response(
                401 if failure == "unauthorized" else 429,
                request=request,
                text="SENSITIVE PROVIDER BODY",
            )
            raise httpx.HTTPStatusError("provider failure", request=request, response=response)

    monkeypatch.setattr(reconciliation, "LambdaCloud", lambda *_: FailingProvider())
    reconciliation.refresh_capacity(force=True)
    result = client.get("/api/v1/capacity").json()
    assert result["status"] == "stale" and result["instances"] == previous["instances"]
    assert "SENSITIVE" not in json.dumps(result) and result["error"]


def test_migration_roundtrip_and_idempotent_seed(test_database):
    from alembic import command
    from alembic.config import Config

    config = Config(str(ROOT / "apps/management/backend/alembic.ini"))
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with session() as db, db.begin():
        assert seed(db, ROOT) == 25
        assert seed(db, ROOT) == 25


def test_local_artifact_checksum_and_path_boundary(tmp_path):
    import hashlib

    from platform_management.integrations.artifacts import LocalArtifactAccess

    root = tmp_path / "artifacts"
    root.mkdir()
    artifact = root / "payload"
    artifact.write_bytes(b"prepared-data")
    checksum = hashlib.sha256(b"prepared-data").hexdigest()
    access = LocalArtifactAccess(root)
    assert access.verify("local://payload", checksum)
    assert not access.verify("local://../outside", checksum)
    assert not access.verify("fixture://payload", checksum)


def test_redeploy_after_group_removal_preserves_historical_runtime_assignment(client):
    ready_serving(client)
    started = post(
        client,
        "/runs",
        {
            "scenario_ref": "writer-assist-baseline@1",
            "group_ids": {"latency-workers@1": "demo-group"},
        },
    ).json()
    settle()
    assert post(client, "/worker-groups/demo-group/delete").status_code == 202
    settle()
    assert client.get("/api/v1/serving-deployments").json()[0]["status"] == "unavailable"
    create(client, identity="replacement-group")
    settle()
    redeployed = post(
        client,
        "/serving-deployments",
        {"id": "gec", "profile_ref": "gec-baseline@1", "group_id": "replacement-group"},
    )
    assert redeployed.status_code == 202, redeployed.text
    settle()
    deployment = client.get("/api/v1/serving-deployments").json()[0]
    assert deployment["group_id"] == "replacement-group" and deployment["status"] == "ready"
    historical = client.get("/api/v1/runs/" + started["resource_id"]).json()
    assert historical["group_ids"] == {"latency-workers@1": "demo-group"}


def test_failed_runner_operation_exposes_execution_error(client):
    ready_serving(client)
    response = post(
        client,
        "/runs",
        {
            "scenario_ref": "writer-assist-baseline@1",
            "group_ids": {"latency-workers@1": "demo-group"},
            "outcome": "failed",
        },
    ).json()
    settle()
    operation = client.get("/api/v1/operations/" + response["operation_id"]).json()["operation"]
    assert (
        operation["status"] == "failed" and operation["error"]["code"] == "simulated_runner_failure"
    )
