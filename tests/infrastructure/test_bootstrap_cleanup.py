"""Failed bootstrap recovery must not bypass a serving worker's drain policy."""

import base64
import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from cluster_models import PeerRegistry
from platform_contracts import HostTask, ProviderFence
from platform_management.host_executor.cleanup import (
    bootstrap_evidence,
    checked_peer,
    perform_cleanup,
)
from platform_management.host_executor.controller import HostController, canonical
from platform_management.services.common import AppError
from test_host_executor import IMAGE, Cluster, task, terminal

UID = "12345678-1234-1234-1234-123456789abc"


def fence(**changes):
    return ProviderFence.model_validate(
        {
            "intent_id": "op-" + "1" * 16,
            "provider_instance_id": "a" * 32,
            "termination_requested_at": datetime.now(UTC) - timedelta(minutes=1),
            "confirmed_at": datetime.now(UTC) - timedelta(seconds=1),
            "observation_sha256": "b" * 64,
            **changes,
        }
    )


def registry(document):
    peers = {
        f"cp-0{i}": {
            "instance_id": f"i-{i}",
            "role": "control_plane",
            "public_ip": f"8.8.4.{i}",
            "private_ip": f"10.40.0.{i}",
            "wireguard_ip": f"10.200.0.{10 + i}",
            "public_key": base64.b64encode(bytes([i]) * 32).decode(),
        }
        for i in range(1, 4)
    }
    peers[document.node_name] = {
        "instance_id": document.provider_instance_id,
        "role": "worker",
        "public_ip": str(document.public_ip),
        "private_ip": str(document.private_ip),
        "wireguard_ip": str(document.wireguard_ip),
        "public_key": base64.b64encode(b"x" * 32).decode(),
    }
    return PeerRegistry(peers=peers, revision=1)


class ControlPlane:
    def __init__(self, document):
        self.calls = []
        self.node = {
            "metadata": {"name": document.node_name, "uid": UID, "resourceVersion": "25"},
            "spec": {
                "providerID": "lambda:///" + document.provider_instance_id,
                "taints": [{"key": "klip.dev/bootstrap", "value": "true", "effect": "NoSchedule"}],
                "unschedulable": True,
            },
        }
        self.pods = []

    def run(self, args):
        self.calls.append(args)
        if args[3:5] == ["get", "node"]:
            return json.dumps(self.node) if self.node else ""
        if args[3:5] == ["get", "pods"]:
            return json.dumps({"items": self.pods})
        assert args[3:5] == ["patch", "node"]
        return "patched"


def failed(controller, document):
    return terminal(
        controller,
        document,
        status="failed",
        cleanup_pending=True,
        error={"code": "bootstrap_failed", "message": "Synthetic failure"},
    )


def test_old_task_digest_remains_compatible_and_fences_are_typed():
    document = task()
    original = document.model_dump(mode="json", exclude={"provider_fence"})
    assert canonical(document) == json.dumps(original, sort_keys=True).encode()
    with pytest.raises(ValueError, match="fence"):
        task("2", action="revoke-bootstrap", previous_task_id=document.id)
    with pytest.raises(ValueError, match="fence"):
        task(provider_fence=fence())
    with pytest.raises(ValueError, match="predates"):
        fence(confirmed_at=datetime.now(UTC) - timedelta(minutes=2))


def test_recovery_is_a_new_same_identity_task_and_requires_fence_permission(tmp_path):
    cluster, original = Cluster(), task()
    controller = HostController(tmp_path, IMAGE, cluster)
    controller.launch(original)
    failed(controller, original)
    check = task("2", action="cleanup-check", previous_task_id=original.id)
    assert controller.launch(check).status == "running"
    terminal(
        controller,
        check,
        bootstrap_fence_permitted=True,
        evidence_sha256="a" * 64,
        node_name=original.node_name,
        node_uid=UID,
    )
    with pytest.raises(AppError, match="remains reserved"):
        controller.launch(task("5"))
    revoke = task(
        "3",
        action="revoke-bootstrap",
        previous_task_id=check.id,
        node_uid=UID,
        provider_fence=fence(),
    )
    assert controller.launch(revoke).status == "running"
    failed(controller, revoke)
    retry = task(
        "4",
        action="revoke-bootstrap",
        previous_task_id=revoke.id,
        node_uid=UID,
        provider_fence=revoke.provider_fence,
    )
    assert controller.launch(retry).status == "running"
    assert len(cluster.posts) == 4  # Each bounded recovery has its own native Job identity.


def test_recovery_cannot_change_uid_or_jump_back_to_join(tmp_path):
    cluster, original = Cluster(), task()
    controller = HostController(tmp_path, IMAGE, cluster)
    controller.launch(original)
    terminal(controller, original)
    check = task("2", action="cleanup-check", previous_task_id=original.id)
    controller.launch(check)
    terminal(controller, check, node_name=original.node_name, node_uid=UID)
    with pytest.raises(AppError, match="transition"):
        controller.launch(task("3", action="join", previous_task_id=check.id, node_uid=UID))
    with pytest.raises(AppError, match="UID"):
        controller.launch(
            task("4", action="revoke-bootstrap", previous_task_id=check.id, provider_fence=fence())
        )
    with pytest.raises(AppError, match="safety check"):
        controller.launch(
            task(
                "5",
                action="revoke-bootstrap",
                previous_task_id=check.id,
                provider_fence=fence(),
                node_uid=UID,
            )
        )


@pytest.mark.parametrize(
    "mutation", ["uid", "provider", "quarantine", "workload", "static", "control-plane"]
)
def test_bootstrap_check_refuses_reused_or_serving_node(mutation):
    document = task("2", action="cleanup-check", previous_task_id=task().id, node_uid=UID)
    cp = ControlPlane(document)
    if mutation == "uid":
        cp.node["metadata"]["uid"] = "different"
    elif mutation == "provider":
        cp.node["spec"]["providerID"] = "lambda:///" + "b" * 32
    elif mutation == "quarantine":
        cp.node["spec"]["taints"] = []
    elif mutation == "control-plane":
        cp.node["metadata"]["labels"] = {"node-role.kubernetes.io/control-plane": ""}
    else:
        cp.pods = [
            {
                "metadata": {"name": "active", "annotations": {"kubernetes.io/config.mirror": "x"}},
                "status": {"phase": "Running"},
            }
        ]
    with pytest.raises(ValueError):
        bootstrap_evidence(cp, document, cordon=True)
    assert not any(call[3] == "patch" for call in cp.calls)


def test_check_cordons_with_uid_and_resource_version_preconditions():
    document = task("2", action="cleanup-check", previous_task_id=task().id)
    cp = ControlPlane(document)
    cp.node["spec"]["unschedulable"] = False
    evidence = bootstrap_evidence(cp, document, cordon=True)
    patch = json.loads(cp.calls[-1][-1])
    assert patch[:2] == [
        {"op": "test", "path": "/metadata/uid", "value": UID},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "25"},
    ]
    assert evidence["node"]["uid"] == UID
    with pytest.raises(ValueError, match="cordon"):
        bootstrap_evidence(cp, document)


def test_fenced_revocation_reconciles_peers_before_uid_guarded_node_delete(tmp_path, monkeypatch):
    import cluster
    import cluster_workers

    document = task(
        "3",
        action="revoke-bootstrap",
        previous_task_id=task("2").id,
        node_uid=UID,
        provider_fence=fence(),
    )
    peers, cp, calls = registry(document), ControlPlane(document), []
    (tmp_path / "cluster").mkdir()
    (tmp_path / "executions" / document.id).mkdir(parents=True)
    monkeypatch.setattr(cluster, "assert_peer_writer", lambda: None)
    monkeypatch.setattr(cluster, "load_hosts", dict)
    monkeypatch.setattr(
        cluster_workers, "remove_host", lambda name: calls.append(("inventory", name))
    )

    def mesh(*args, **kwargs):
        saved = json.loads((tmp_path / "cluster/peers.json").read_text())
        assert document.node_name not in saved["peers"] and saved["revision"] == 2
        calls.append(("mesh", args[0]))

    monkeypatch.setattr(cluster, "ansible", mesh)
    monkeypatch.setattr(
        cluster_workers, "delete_node", lambda req: calls.append(("delete", req.node_uid))
    )
    observed = perform_cleanup(tmp_path, document, peers, cp)
    assert [c[0] for c in calls] == ["inventory", "mesh", "delete"]
    assert calls[-1][1] == UID and observed["bootstrap_cleanup_complete"]
    assert not observed["bootstrap_fence_permitted"] and observed["evidence_sha256"]
    assert all(call[1] == "kubectl" for call in cp.calls)  # No access to the fenced GPU host.


def test_peer_collision_and_changed_identity_refuse_revocation():
    document = task()
    original = registry(document).model_dump(mode="json")
    for change in ("identity", "collision"):
        peers = copy.deepcopy(original)
        if change == "identity":
            peers["peers"][document.node_name]["instance_id"] = "b" * 32
        else:
            peers["peers"]["gpu-other"] = peers["peers"].pop(document.node_name)
        with pytest.raises(ValueError):
            checked_peer(PeerRegistry.model_validate(peers), document)


def test_revoke_contract_rejects_a_different_provider_fence():
    with pytest.raises(ValueError, match="fence"):
        HostTask.model_validate(
            {
                **task().model_dump(mode="json"),
                "action": "revoke-bootstrap",
                "previous_task_id": task("2").id,
                "provider_fence": fence(provider_instance_id="b" * 32),
            }
        )
