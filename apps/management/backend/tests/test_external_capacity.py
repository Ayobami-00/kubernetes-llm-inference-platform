"""Synthetic observations exercise B handoffs; these tests never contact a provider."""

import copy
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from platform_contracts import RunSnapshot
from platform_management.api.schemas import RunLaunch
from platform_management.db.models import Observation, Resource
from platform_management.db.session import session
from platform_management.jobs.reconciliation import reconcile_expiry
from platform_management.services.common import digest, now
from platform_management.services.readiness import assess
from platform_management.settings import get_settings
from test_integrations import post, receipt

ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize(
    "resources",
    [
        {"requests": {"cpu": "2", "memory": "1Gi"}, "limits": {"cpu": "1", "memory": "2Gi"}},
        {"requests": {"cpu": "0", "memory": "1Gi"}, "limits": {"cpu": "2", "memory": "2Gi"}},
        {"requests": {"cpu": "1", "memory": "4Gi"}, "limits": {"cpu": "2", "memory": "2Gi"}},
    ],
)
def test_runner_resource_reservations_reject_impossible_limits(resources):
    from platform_management.settings import Settings

    with pytest.raises(ValueError, match="runner requests"):
        Settings(runner_resources=resources)


def example(retained=False):
    stamp = now()
    inventory = json.loads((ROOT / "contracts/fixtures/inventories/cluster.json").read_text())
    inventory.update(
        fixture=False, snapshot_id="external-test-inventory", captured_at=stamp.isoformat()
    )
    for node in inventory["nodes"]:
        node["observed_at"] = stamp.isoformat()
        if node["role"] == "gpu_worker":
            node["managed_by"] = "external"
            if node["worker_group_ref"] == "latency-workers@1":
                node["worker_group_ref"] = "latency-workers@2"
    group = json.loads((ROOT / "contracts/fixtures/worker-groups/ready.json").read_text())
    group["fixture"] = False
    node = next(n for n in inventory["nodes"] if n.get("worker_group_ref") == "latency-workers@2")
    group["status"]["members"][0].update(
        provider_instance_id=node["provider_instance_id"],
        node_name=node["kubernetes"]["name"],
        node_uid=node["kubernetes"]["uid"],
    )
    group["spec"].update(revision=2, instance_type="test-verified-sku", region="test-region")
    group["status"].update(
        worker_group_ref="latency-workers@2",
        observed_at=stamp.isoformat(),
        allocated_at=(stamp - timedelta(minutes=1)).isoformat(),
        expires_at=(stamp + timedelta(minutes=89)).isoformat(),
    )
    if retained:
        group["spec"].update(capacity_lifecycle="externally_retained", ttl_minutes=None)
        group["status"].update(capacity_lifecycle="externally_retained", expires_at=None)
    snapshot = json.loads(
        (ROOT / "contracts/fixtures/runs/writer-assist/completed-passing.json").read_text()
    )["snapshot"]
    profile = snapshot["serving_profiles"][0]
    profile.update(revision=2, draft=False)
    profile["engine"]["image"] = "test-vllm@sha256:" + "a" * 64
    profile["model"].update(revision="b" * 40, tokenizer_revision="b" * 40)
    document = dict(
        id="test-external-handoff",
        inventory_id=inventory["snapshot_id"],
        worker_groups=[dict(id="b-latency", group=group)],
        deployments=[
            dict(
                id="b-gec",
                group_id="b-latency",
                profile=profile,
                kubernetes_uid="test-vllm-uid",
                endpoint="http://vllm.llm-d:8000/v1/chat/completions",
            )
        ],
    )
    snapshot["worker_groups"] = [group["spec"]]
    snapshot["serving_profiles"] = [profile]
    snapshot["inventory"] = inventory
    snapshot["bindings"][0].update(
        serving_profile_ref="gec-baseline@2",
        worker_group_ref="latency-workers@2",
        deployment_id="b-gec",
    )
    return inventory, document, RunSnapshot.model_validate(snapshot)


def onboard(client, retained=False):
    inventory, document, snapshot = example(retained)
    assert (
        receipt(client, post(client, "inventory", inventory, provenance="measured"))["status"]
        == "completed"
    )
    imported = receipt(client, post(client, "external-capacity", document, provenance="measured"))
    assert imported["status"] == "completed", imported
    return inventory, document, snapshot


@pytest.mark.parametrize("retained", [False, True])
def test_onboarding_is_idempotent_and_external_resources_are_read_only(client, retained):
    _, document, _ = onboard(client, retained)
    assert (
        receipt(client, post(client, "external-capacity", document, provenance="measured"))[
            "status"
        ]
        == "completed"
    )
    group = client.get("/api/v1/worker-groups").json()[0]
    assert group["ownership"] == "external" and group["mode"] == "real"
    for action, body in [("renew", {"additional_minutes": 30}), ("delete", {})]:
        response = client.post(
            f"/api/v1/worker-groups/b-latency/{action}",
            json=body,
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert (
            response.status_code == 409 and response.json()["error"]["code"] == "external_ownership"
        )
    response = client.post(
        "/api/v1/serving-deployments",
        json=dict(id="other", profile_ref="gec-baseline@1", group_id="b-latency"),
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 409
    changed = copy.deepcopy(document)
    changed["deployments"][0]["kubernetes_uid"] = "replacement"
    assert (
        receipt(client, post(client, "external-capacity", changed, provenance="measured"))["status"]
        == "failed"
    )


def test_missing_inventory_and_member_mismatch_fail_closed(client):
    inventory, document, _ = example()
    assert (
        receipt(client, post(client, "external-capacity", document, provenance="measured"))[
            "status"
        ]
        == "failed"
    )
    receipt(client, post(client, "inventory", inventory, provenance="measured"))
    document["worker_groups"][0]["group"]["status"]["members"][0]["node_uid"] = "different"
    assert (
        receipt(client, post(client, "external-capacity", document, provenance="measured"))[
            "status"
        ]
        == "failed"
    )
    assert client.get("/api/v1/worker-groups").json() == []


def test_expiry_does_not_delete_external_capacity(client):
    onboard(client)
    with session() as db, db.begin():
        row = db.get(Resource, "b-latency")
        value = copy.deepcopy(row.payload)
        value["group"]["status"].update(
            allocated_at=(now() - timedelta(hours=2)).isoformat(),
            expires_at=(now() - timedelta(minutes=1)).isoformat(),
        )
        row.payload = value
    reconcile_expiry()
    with session() as db:
        row = db.get(Resource, "b-latency")
        assert (
            row.active_operation_id is None and row.payload["group"]["status"]["phase"] == "ready"
        )


@pytest.mark.parametrize(
    "mismatch", [None, "retained", "image", "uid", "profile", "gpu", "stale", "lease"]
)
def test_live_evidence_and_remaining_lease_are_required(client, mismatch, monkeypatch):
    _, _, snapshot = onboard(client, retained=mismatch == "retained")
    monkeypatch.setenv("MANAGEMENT_KUBE_API_URL", str(snapshot.inventory.cluster.api_endpoint))
    get_settings.cache_clear()
    profile = snapshot.serving_profiles[0]
    live = dict(
        status="fresh",
        observed_at=now().isoformat(),
        nodes=[
            dict(uid=m["node_uid"], ready=True, gpus=1)
            for m in example()[1]["worker_groups"][0]["group"]["status"]["members"]
        ],
        deployments=[
            dict(
                id="b-gec",
                uid="test-vllm-uid",
                ready=True,
                profile_ref=profile.ref,
                replicas=1,
                images=[profile.engine.image],
                profile_sha256=digest(profile.model_dump(mode="json")),
            )
        ],
    )
    if mismatch == "image":
        live["deployments"][0]["images"] = ["wrong"]
    if mismatch == "uid":
        live["deployments"][0]["uid"] = "wrong"
    if mismatch == "profile":
        live["deployments"][0]["profile_sha256"] = "wrong"
    if mismatch == "gpu":
        live["nodes"][0]["gpus"] = 0
    if mismatch == "stale":
        live["observed_at"] = (now() - timedelta(minutes=10)).isoformat()
    with session() as db, db.begin():
        db.merge(Observation(id="cluster-readiness", payload=live))
        if mismatch == "lease":
            row = db.get(Resource, "b-latency")
            value = copy.deepcopy(row.payload)
            value["group"]["status"]["expires_at"] = (now() + timedelta(seconds=1)).isoformat()
            row.payload = value
    with session() as db:
        report = assess(
            db,
            RunLaunch(
                scenario_ref=snapshot.scenario.ref,
                mode="real",
                group_ids={"latency-workers@2": "b-latency"},
            ),
            snapshot,
        )
    checks = {c.code: c.status for c in report.checks}
    if mismatch in {None, "retained"}:
        assert (
            checks["gpu-latency-workers"]
            == checks["lease-latency-workers"]
            == checks["serving-b-gec"]
            == "ready"
        )
    elif mismatch in {"image", "uid", "profile", "stale"}:
        assert checks["serving-b-gec"] != "ready"
    elif mismatch == "gpu":
        assert checks["gpu-latency-workers"] != "ready"
    else:
        assert checks["lease-latency-workers"] != "ready"
    assert not report.executable  # Deployment evidence never bypasses draft/preparation checks.
    get_settings.cache_clear()
