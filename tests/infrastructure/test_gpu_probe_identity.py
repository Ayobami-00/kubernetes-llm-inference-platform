"""Pod sandbox lookup and cleanup cannot adopt another execution's resources."""

import base64
import json
from datetime import UTC, datetime, timedelta

import cluster
import node_agent
import pytest
from platform_contracts import HostTask
from platform_management.host_executor.inspection_probes import (
    HASH_ANNOTATION,
    cleanup_chain,
    manifests,
)

UID = "11111111-2222-3333-4444-555555555555"


def test_mtu_check_selects_the_exact_ready_pod_uid(monkeypatch):
    calls = []

    def command(*args):
        calls.append(args)
        if "pods" in args:
            return json.dumps(
                {
                    "items": [
                        {"id": "old", "metadata": {"uid": "another"}, "state": "SANDBOX_READY"},
                        {"id": "selected", "metadata": {"uid": UID}, "state": "SANDBOX_READY"},
                    ]
                }
            )
        if "inspectp" in args:
            assert args[-1] == "selected"
            return json.dumps({"info": {"pid": 4321}, "status": {"metadata": {"uid": UID}}})
        if "link" in args:
            return '[{"mtu":1420}]'
        if "route" in args:
            return '[{"dst":"default","metrics":[{"mtu":1370}]}]'
        raise AssertionError(args)

    monkeypatch.setattr(node_agent, "command", command)
    assert node_agent.pod_network(UID) == {"pod_interface_mtu": 1420, "pod_route_mtu": 1370}
    assert all("old" not in args for args in calls)


def test_mtu_check_rejects_a_sandbox_replaced_after_listing(monkeypatch):
    def command(*args):
        if "pods" in args:
            return json.dumps(
                {"items": [{"id": "id", "metadata": {"uid": UID}, "state": "SANDBOX_READY"}]}
            )
        return '{"status":{"metadata":{"uid":"replacement"}}, "info":{"pid":1}}'

    monkeypatch.setattr(node_agent, "command", command)
    with pytest.raises(ValueError, match="identity changed"):
        node_agent.pod_network(UID)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/namespaces/kube-system/pods/kube-apiserver-cp-01",
        "/api/v1/namespaces/gpu-system/pods/someone-else",
        "/api/v1/namespaces/klip-acceptance/pods/../../../nodes/cp-01",
    ],
)
def test_probe_cleanup_never_broadens_existing_cpu_deletion_scope(path):
    with pytest.raises(ValueError, match="cleanup target"):
        cluster.delete_owned_resource(path, UID)


def test_explicit_remove_cleans_frozen_probes_and_rejects_changed_manifest(tmp_path, monkeypatch):
    from platform_management.host_executor import inspection_probes

    inspect = HostTask(
        id="host-" + "a" * 24,
        action="inspect",
        provider_instance_id="b" * 32,
        node_name="gpu-" + "b" * 32,
        node_uid=UID,
        public_ip="8.8.8.8",
        private_ip="192.168.1.2",
        wireguard_ip="10.200.0.67",
        trusted_host_key="ssh-ed25519 "
        + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"x" * 32).decode(),
        registry_revision=13,
        deadline=datetime.now(UTC) + timedelta(minutes=20),
        previous_task_id="host-" + "c" * 24,
    )
    prepare = inspect.model_copy(
        update={
            "id": inspect.previous_task_id,
            "action": "prepare",
            "previous_task_id": None,
            "node_uid": None,
        }
    )
    remove = inspect.model_copy(
        update={"id": "host-" + "d" * 24, "action": "remove", "previous_task_id": inspect.id}
    )
    for task in (prepare, inspect):
        path = tmp_path / "executions" / task.id
        path.mkdir(parents=True)
        (path / "task.json").write_text(task.model_dump_json())
    documents = manifests(
        inspect, ["cp-01", "cp-02", "cp-03", "platform-01", "platform-02", "platform-03"]
    )
    path = tmp_path / "executions" / inspect.id / "probe-manifests.json"
    path.write_text(json.dumps(documents))
    calls = []
    monkeypatch.setattr(inspection_probes, "cleanup", lambda cp, docs: calls.append(docs))
    cleanup_chain(tmp_path, remove, None)
    assert len(calls) == 1
    documents[0]["spec"]["containers"][0]["args"] = ["changed"]
    assert documents[0]["metadata"]["annotations"][HASH_ANNOTATION]
    path.write_text(json.dumps(documents))
    with pytest.raises(ValueError, match="input hash"):
        cleanup_chain(tmp_path, remove, None)
    assert len(calls) == 1
