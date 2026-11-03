import copy
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from platform_contracts import RunSnapshot
from platform_management.db.models import (
    ExecutionBinding,
    Job,
    PreparedRun,
    Resource,
    RunRow,
)
from platform_management.db.session import session
from platform_management.integrations.artifacts import LocalArtifactAccess
from platform_management.integrations.cluster_client import ClusterClient
from platform_management.integrations.job_runner import KubernetesJobRunner
from platform_management.integrations.registry import configuration
from platform_management.jobs.worker import tick
from platform_management.providers.base import TransientProviderError
from platform_management.services.common import now
from platform_management.settings import get_settings
from sqlalchemy import update
from sqlalchemy.exc import DBAPIError

ROOT = Path(__file__).resolve().parents[4]


def historical():
    return json.loads(
        (ROOT / "contracts/fixtures/runs/writer-assist/completed-passing.json").read_text()
    )


def snapshot():
    # Fixture filenames are located by their actual contract status, not assumed naming.
    for path in (ROOT / "contracts/fixtures/runs").glob("*/*.json"):
        data = json.loads(path.read_text())
        if (
            data["status"] == "completed"
            and data["snapshot"]["scenario"]["id"] == "writer-assist-baseline"
        ):
            return data
    raise AssertionError("Shared fixture missing")


def settle():
    for _ in range(80):
        with session() as db, db.begin():
            db.execute(update(Job).where(Job.state == "pending").values(due_at=now()))
        if not tick():
            return
    raise AssertionError("Internal jobs did not settle")


def post(client, kind, document, key=None, provenance="synthetic", source="fixture-stream-a"):
    return client.post(
        "/api/v1/imports/" + kind,
        json={"document": document, "source": source, "provenance": provenance},
        headers={"Idempotency-Key": key or uuid4().hex},
    )


def receipt(client, response):
    assert response.status_code == 202, response.text
    settle()
    result = client.get(response.headers["Location"])
    assert result.status_code == 200
    return result.json()


def test_import_replay_conflict_and_terminal_immutability(client):
    run = snapshot()
    run["id"] = "external-fixture-result"
    key = uuid4().hex
    first = post(client, "runs", run, key)
    assert post(client, "runs", run, key).json() == first.json()
    other = copy.deepcopy(run)
    other["result"]["notes"] = "Different immutable external result"
    assert post(client, "runs", other, key).status_code == 409
    assert receipt(client, first)["status"] == "completed"
    assert receipt(client, post(client, "runs", other))["status"] == "failed"
    result = client.get("/api/v1/runs/" + run["id"]).json()
    assert result["source"] == "fixture-stream-a" and result["provenance"] == "synthetic"
    before = copy.deepcopy(result["run"]["snapshot"])
    with pytest.raises(DBAPIError), session() as db, db.begin():
        row = db.get(RunRow, run["id"])
        value = copy.deepcopy(row.payload)
        value["result"]["notes"] = "Must not rewrite history"
        row.payload = value
        db.flush()
    assert client.get("/api/v1/runs/" + run["id"]).json()["run"]["snapshot"] == before
    graph = client.get("/api/v1/runs/" + run["id"] + "/topology").json()
    assert graph["historical"] and any(n["kind"] == "customer" for n in graph["nodes"])


def test_import_validation_and_provenance(client):
    run = snapshot()
    assert post(client, "runs", run, provenance="measured").status_code == 422
    run["id"] = "external-measurement"
    run["fixture"] = False
    result = receipt(
        client, post(client, "runs", run, provenance="unverified", source="external-report")
    )
    assert result["status"] == "completed"
    shown = client.get("/api/v1/runs/external-measurement").json()
    assert shown["mode"] == "real" and shown["provenance"] == "unverified"
    run["status"] = "running"
    run["result"] = None
    run["finished_at"] = None
    assert post(client, "runs", run, provenance="unverified").status_code == 422
    unknown = {**snapshot(), "invented_measurement": True}
    assert post(client, "runs", unknown).status_code == 422


def test_dataset_and_prepared_bundle_preserve_existing_references(client):
    original = client.get("/api/v1/customers").json()
    snap = snapshot()["snapshot"]
    dataset = copy.deepcopy(snap["datasets"][0])
    dataset["revision"] += 1
    result = receipt(client, post(client, "datasets", dataset))
    assert result["status"] == "completed"
    assert client.get("/api/v1/customers").json() == original
    imported = client.get("/api/v1/datasets").json()
    assert any(
        d["id"] == dataset["id"] and d["current_revision"] == dataset["revision"] for d in imported
    )
    bundle = {"id": "fixture-prepared", "fixture": True, "snapshot": snap}
    accepted = receipt(client, post(client, "prepared-runs", bundle))
    assert accepted["status"] == "completed"
    assert client.get("/api/v1/prepared-runs").json()[0]["snapshot"] == snap
    report = client.post(
        "/api/v1/runs/preflight",
        json={
            "scenario_ref": "writer-assist-baseline@1",
            "prepared_run_id": "fixture-prepared",
            "mode": "real",
        },
    ).json()
    assert not report["executable"]
    assert any(
        c["code"] == "measurement-source" and c["status"] == "blocked" for c in report["checks"]
    )
    with pytest.raises(DBAPIError), session() as db, db.begin():
        row = db.get(PreparedRun, "fixture-prepared")
        row.snapshot = {**row.snapshot, "invented": True}
        db.flush()


def test_inventory_history_partial_projection_and_stale_pointer(client):
    path = next((ROOT / "contracts/fixtures/inventories").glob("*.json"))
    inventory = json.loads(path.read_text())
    inventory["snapshot_id"] = "current-inventory"
    inventory["captured_at"] = now().isoformat()
    from datetime import timedelta

    for node in inventory["nodes"]:
        node["observed_at"] = (now() - timedelta(minutes=10)).isoformat()
    assert receipt(client, post(client, "inventory", inventory))["status"] == "completed"
    older = copy.deepcopy(inventory)
    older["snapshot_id"] = "older-inventory"
    from datetime import timedelta

    older["captured_at"] = (now() - timedelta(minutes=5)).isoformat()
    assert receipt(client, post(client, "inventory", older))["status"] == "completed"
    rows = client.get("/api/v1/inventory").json()
    assert next(r for r in rows if r["id"] == "current-inventory")["current"]
    assert not next(r for r in rows if r["id"] == "older-inventory")["current"]
    partial = {
        "schema_version": "foundation.v1",
        "captured_at": now().isoformat(),
        "allocation": {"cluster_id": inventory["cluster"]["id"]},
        "network": inventory["network"],
        "nodes": [n for n in inventory["nodes"] if n["role"] == "control_plane"],
    }
    imported = receipt(client, post(client, "inventory", partial))
    assert imported["status"] == "completed"
    record = client.get("/api/v1/inventory/" + imported["result"]["resource_id"]).json()
    assert not record["complete"] and "ca_bundle_ref" not in record["document"]
    graph = client.get("/api/v1/topology").json()
    assert any(n["kind"] == "infrastructure-node" for n in graph["nodes"])
    partial["allocation"]["private_key"] = "must-not-persist"
    assert post(client, "inventory", partial).status_code == 422


def test_artifact_verification_and_download_rechecks_contents(client, tmp_path, monkeypatch):
    monkeypatch.setenv("MANAGEMENT_ARTIFACTS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    path = tmp_path / "prepared.jsonl"
    path.write_bytes(b'{"synthetic":true}\n')
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    snap = snapshot()["snapshot"]
    dataset = copy.deepcopy(snap["datasets"][0])
    dataset["id"], dataset["revision"] = "fixture-staged-dataset", 1
    dataset["prepared"] = {
        "uri": path.as_uri(),
        "sha256": sha,
        "record_count": 1,
        "tokenizer_revision": "fixture-tokenizer",
        "prompt_template_sha256": "0" * 64,
        "max_rendered_input_tokens": 1,
    }
    imported = receipt(client, post(client, "datasets", dataset))
    assert imported["status"] == "completed"
    check = imported["result"]["artifacts"][0]
    assert check["status"] == "verified"
    response = client.get("/api/v1/artifacts/" + check["id"])
    assert response.status_code == 200 and response.content == path.read_bytes()
    path.write_text("changed")
    assert client.get("/api/v1/artifacts/" + check["id"]).status_code == 409
    access = LocalArtifactAccess(tmp_path)
    outside = tmp_path.parent / "fixture-outside"
    outside.write_text("outside")
    (tmp_path / "escape").symlink_to(outside)
    for uri in [
        outside.as_uri(),
        "file://" + str(tmp_path) + "/%2e%2e/fixture-outside",
        (tmp_path / "escape").as_uri(),
        "https://127.0.0.1/private",
    ]:
        assert not access.verify(uri, sha)
    get_settings.cache_clear()


def test_import_body_limit_and_cross_workflow_keys(client, monkeypatch):
    monkeypatch.setenv("MANAGEMENT_IMPORT_MAX_BYTES", "50")
    get_settings.cache_clear()
    assert post(client, "runs", snapshot()).status_code == 413
    get_settings.cache_clear()


def test_old_binding_does_not_follow_new_configuration(client, monkeypatch):
    response = client.post(
        "/api/v1/worker-groups",
        json={"id": "frozen-adapter-group", "spec_ref": "latency-workers@1"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 202
    with session() as db:
        row = db.get(Resource, "frozen-adapter-group")
        binding_id = row.binding_id
        assert db.get(ExecutionBinding, binding_id).payload["provider"] == "simulated:v1"
    monkeypatch.setenv("MANAGEMENT_RUNNER_NAMESPACE", "changed-namespace")
    get_settings.cache_clear()
    settle()
    with session() as db:
        row = db.get(Resource, "frozen-adapter-group")
        assert row.binding_id == binding_id and row.payload["group"]["status"]["phase"] == "ready"
    get_settings.cache_clear()


def mock_runner(tmp_path):
    config = {
        **configuration(),
        "runner": "kubernetes-fixture:v1",
        "artifact_root": str(tmp_path),
        "runner_image": "fixture:test",
        "artifact_claim": "artifacts",
        "kube_api_url": "https://cluster.example.test",
        "kube_token_file": "/absent",
        "kube_ca_file": "/absent",
    }
    state = {"job": None, "creates": 0, "pods": [], "timeout": False}

    def handler(request):
        if request.url.path.endswith("/pods"):
            return httpx.Response(200, json={"items": state["pods"]})
        if request.method == "POST":
            state["creates"] += 1
            state["job"] = json.loads(request.content)
            state["job"]["metadata"]["uid"] = "fixture-job-uid"
            if state["timeout"]:
                state["timeout"] = False
                raise httpx.ReadTimeout("ambiguous accepted create")
            return httpx.Response(201, json=state["job"])
        if request.method == "DELETE":
            state["job"] = None
            return httpx.Response(200, json={})
        return httpx.Response(200, json=state["job"]) if state["job"] else httpx.Response(404)

    client = ClusterClient(config, transport=httpx.MockTransport(handler))
    return KubernetesJobRunner(config, "fixture-run", client=client), state


def test_ambiguous_job_creation_reconciles_and_missing_uid_is_not_recreated(tmp_path):
    runner, state = mock_runner(tmp_path)
    handle = runner.prepare(RunSnapshot.model_validate(snapshot()["snapshot"]), "op-fixture")
    state["timeout"] = True
    with pytest.raises(TransientProviderError):
        runner.ensure_started(handle)
    uid = runner.ensure_started(handle)
    assert state["creates"] == 1 and uid == "fixture-job-uid"
    with pytest.raises(ValueError):
        runner.observe(handle, "wrong-uid")
    state["job"] = None
    observation = runner.observe(handle, uid)
    assert observation.status == "failed" and observation.error.code == "runner_lost"
    assert state["creates"] == 1
    runner.close()


def test_job_exit_requires_a_valid_shared_result_and_confirmed_stop(tmp_path):
    runner, state = mock_runner(tmp_path)
    handle = runner.prepare(RunSnapshot.model_validate(snapshot()["snapshot"]), "op-fixture")
    uid = runner.ensure_started(handle)
    state["job"]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    assert runner.observe(handle, uid).error.code == "invalid_result"
    directory = runner.directory(handle)
    (directory / "result.json").write_text(json.dumps(snapshot()["result"]))
    assert runner.observe(handle, uid).status == "completed"
    state["pods"] = [{"status": {"phase": "Running"}}]
    assert not runner.stop(handle, uid)
    state["pods"] = []
    assert runner.stop(handle, uid)
    assert (directory / "cancel-requested").exists()
    runner.close()


def test_duplicate_bridge_processes_invoke_benchmark_only_once(tmp_path):
    runner, _ = mock_runner(tmp_path)
    handle = runner.prepare(RunSnapshot.model_validate(snapshot()["snapshot"]), "op-fixture")
    directory = runner.directory(handle)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    counter = directory / "invocations"
    manifest["argv"] = [
        sys.executable,
        "-c",
        "from pathlib import Path; import time; "
        f"p=Path({str(counter)!r}); "
        "p.write_text(p.read_text()+'x' if p.exists() else 'x'); time.sleep(.4)",
    ]
    manifest_path.write_text(json.dumps(manifest))
    command = [
        sys.executable,
        str(directory / "runner_bridge.py"),
        "--manifest",
        str(manifest_path),
    ]
    first = subprocess.Popen(command)
    second = subprocess.Popen(command)
    assert first.wait(timeout=10) == 0 and second.wait(timeout=10) == 0
    assert counter.read_text() == "x"
    assert subprocess.run(command, timeout=5).returncode == 0
    assert counter.read_text() == "x"
    runner.close()


def test_restart_after_started_marker_never_starts_another_clock(tmp_path):
    runner, _ = mock_runner(tmp_path)
    handle = runner.prepare(RunSnapshot.model_validate(snapshot()["snapshot"]), "op-fixture")
    directory = runner.directory(handle)
    (directory / "started").mkdir()
    command = [
        sys.executable,
        str(directory / "runner_bridge.py"),
        "--manifest",
        str(directory / "manifest.json"),
    ]
    process = subprocess.Popen(command)
    time.sleep(0.2)
    assert not (directory / "fixture-invocations.log").exists()
    process.terminate()
    assert process.wait(timeout=5) == 130
    runner.close()


def test_legacy_upgrade_preserves_pending_work_and_frozen_snapshots(test_database):
    from alembic import command
    from alembic.config import Config
    from platform_management.db.session import engine
    from sqlalchemy import text

    schema = "migration_" + uuid4().hex
    with engine().connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET search_path TO "{schema}"'))
        connection.commit()
        config = Config(str(ROOT / "apps/management/backend/alembic.ini"))
        config.attributes["connection"] = connection
        try:
            command.upgrade(config, "0001")
            connection.execute(
                text(
                    "INSERT INTO resources VALUES "
                    "('old-group','worker_group','{\"marker\":\"allocated\"}','old-op')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO operations VALUES "
                    "('old-op','old-group','{\"status\":\"running\"}')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO jobs "
                    "(id,state,owner,attempt,checkpoint) VALUES "
                    "('old-op','pending',NULL,2,'{\"stage\":\"joining\"}')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO runs VALUES "
                    "('old-run','{\"revision\": 1}',"
                    '\'{"status":"completed","result":"historical"}\',\'{}\')'
                )
            )
            connection.commit()
            for _ in range(2):
                command.upgrade(config, "head")
                row = connection.execute(
                    text(
                        "SELECT operation_id,binding_id,attempt,checkpoint "
                        "FROM jobs WHERE id='old-op'"
                    )
                ).one()
                assert tuple(row[:3]) == ("old-op", "legacy-simulation-v1", 2)
                assert row.checkpoint == {"stage": "joining"}
                assert connection.scalar(text("SELECT snapshot FROM runs WHERE id='old-run'")) == {
                    "revision": 1
                }
                assert (
                    connection.scalar(
                        text("SELECT payload->>'marker' FROM resources WHERE id='old-group'")
                    )
                    == "allocated"
                )
                connection.commit()
            command.downgrade(config, "0001")
            command.upgrade(config, "head")
            assert connection.scalar(text("SELECT binding_id FROM runs WHERE id='old-run'")) == (
                "legacy-simulation-v1"
            )
            connection.commit()
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            connection.commit()


def test_concurrent_import_replays_share_one_durable_job(client):
    from concurrent.futures import ThreadPoolExecutor

    from platform_management.api.import_schemas import RunImport
    from platform_management.db.models import ImportReceipt
    from platform_management.services.imports import submit_import
    from sqlalchemy import func, select

    document = snapshot()
    document["id"] = "concurrent-external-result"
    body = RunImport(document=document, source="concurrency-fixture", provenance="synthetic")
    key = uuid4().hex

    def submit(_):
        with session() as db, db.begin():
            return submit_import(db, "runs", body, key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        identities = list(pool.map(submit, range(2)))
    assert identities[0] == identities[1]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: tick(), range(2)))
    with session() as db:
        assert db.scalar(select(func.count()).select_from(ImportReceipt)) == 1
        assert db.get(ImportReceipt, identities[0]).status == "completed"
        assert db.get(Job, identities[0]).state == "completed"


def test_stale_cluster_observation_never_reports_ready(client):
    from datetime import timedelta

    from platform_management.db.models import Observation

    with session() as db, db.begin():
        db.add(
            Observation(
                id="cluster-readiness",
                payload={
                    "status": "fresh",
                    "observed_at": (now() - timedelta(minutes=10)).isoformat(),
                    "nodes": [],
                    "deployments": [],
                },
            )
        )
    report = client.get("/api/v1/integrations").json()
    assert next(c for c in report["checks"] if c["code"] == "cluster")["status"] == "stale"


def test_job_credentials_are_server_selected_references_without_api_token(tmp_path):
    runner, _ = mock_runner(tmp_path)
    runner.binding["runner_secret_refs"] = ["inference-access"]
    handle = runner.prepare(RunSnapshot.model_validate(snapshot()["snapshot"]), "op-fixture")
    pod = runner.template(handle)["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["volumes"][-1]["secret"]["secretName"] == "inference-access"
    assert pod["containers"][0]["volumeMounts"][-1]["readOnly"] is True
    assert pod["restartPolicy"] == "Never"
    runner.binding["runner_secret_refs"] = ["../../private"]
    with pytest.raises(ValueError):
        runner.template(handle)
    runner.close()


def test_cluster_observations_require_current_generation_and_positive_replicas(monkeypatch):
    from platform_management.db.models import Observation
    from platform_management.integrations.observations import refresh_cluster

    deployments = [
        {
            "metadata": {
                "name": name,
                "uid": name,
                "generation": generation,
                "labels": {"klip.dev/deployment-id": name},
                "annotations": {"klip.dev/serving-profile": "gec-baseline@1"},
            },
            "spec": {"replicas": replicas},
            "status": {"observedGeneration": observed, "availableReplicas": available},
        }
        for name, generation, observed, replicas, available in (
            ("ready", 2, 2, 1, 1),
            ("old-generation", 2, 1, 1, 1),
            ("scaled-zero", 1, 1, 0, 0),
            ("not-available", 1, 1, 2, 1),
        )
    ]

    class Client:
        def __init__(self, _):
            pass

        def close(self):
            pass

        def request(self, method, path):
            return {"items": [] if path == "api/v1/nodes" else deployments}

    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "cluster.example.test")
    monkeypatch.setattr("platform_management.integrations.observations.ClusterClient", Client)
    refresh_cluster()
    with session() as db:
        data = db.get(Observation, "cluster-readiness").payload
    assert {d["id"] for d in data["deployments"] if d["ready"]} == {"ready"}
    assert data["status"] == "fresh"


def test_accepted_import_is_committed_before_response_headers_are_sent(client):
    import asyncio

    from platform_management.api.app import app
    from platform_management.db.models import ImportReceipt
    from sqlalchemy import select

    key = uuid4().hex
    checked = []

    async def audited(scope, receive, send):
        async def verify_send(message):
            if message["type"] == "http.response.start":
                assert message["status"] == 202
                with session() as db:
                    row = db.scalar(
                        select(ImportReceipt).where(ImportReceipt.submission_key == key)
                    )
                    assert row is not None, (
                        "Accepted response escaped before the transaction committed"
                    )
                    assert db.get(Job, row.id) is not None
                    checked.append(row.id)
            await send(message)

        await app(scope, receive, verify_send)

    async def submit():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=audited),
            base_url="http://localhost",
            cookies=client.cookies,
        ) as caller:
            result = await caller.post(
                "/api/v1/imports/runs",
                json={
                    "document": snapshot(),
                    "source": "commit-boundary",
                    "provenance": "synthetic",
                },
                headers={"X-CSRF-Token": client.headers["X-CSRF-Token"], "Idempotency-Key": key},
            )
            assert result.json()["import_id"] == checked[0]

    asyncio.run(submit())
