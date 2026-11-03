"""An API fault may affect only a Ready, owned replica in the active release."""

import copy
import json
from unittest.mock import Mock

import pytest
from management_api_failure import select_target


def allocation():
    deployment = {
        "metadata": {"name": "klip-api", "uid": "deployment-one", "generation": 2},
        "spec": {"replicas": 2},
        "status": {
            "observedGeneration": 2,
            "readyReplicas": 2,
            "availableReplicas": 2,
            "updatedReplicas": 2,
        },
    }
    replicasets = [
        {
            "metadata": {
                "uid": "rs-one",
                "ownerReferences": [
                    {"kind": "Deployment", "uid": "deployment-one", "controller": True}
                ],
            }
        }
    ]
    pods = [
        {
            "metadata": {
                "name": "klip-api-1234-" + suffix,
                "uid": suffix,
                "namespace": "klip",
                "ownerReferences": [{"kind": "ReplicaSet", "uid": "rs-one", "controller": True}],
            },
            "spec": {"nodeName": node, "containers": [{"image": "pinned-image"}]},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
        for suffix, node in [("abcde", "platform-01"), ("bcdef", "platform-02")]
    ]
    return deployment, pods, replicasets


@pytest.mark.parametrize(
    "fault", [None, "readiness", "owner", "placement", "image", "generation", "namespace"]
)
def test_api_fault_rejects_unsafe_or_unowned_allocations(fault):
    deployment, pods, replicasets = allocation()
    if fault == "readiness":
        pods[0]["status"]["conditions"][0]["status"] = "False"
    if fault == "owner":
        replicasets[0]["metadata"]["ownerReferences"][0]["uid"] = "other"
    if fault == "placement":
        pods[0]["spec"]["nodeName"] = "cp-01"
    if fault == "image":
        pods[0]["spec"]["containers"][0]["image"] = "other"
    if fault == "generation":
        deployment["status"]["observedGeneration"] = 1
    if fault == "namespace":
        pods[0]["metadata"]["namespace"] = "other"
    before = copy.deepcopy(pods)
    if fault:
        with pytest.raises(ValueError):
            select_target(
                deployment, pods, replicasets, "pinned-image", ["platform-01", "platform-02"]
            )
    else:
        selected = select_target(
            deployment, pods, replicasets, "pinned-image", ["platform-01", "platform-02"]
        )
        assert selected == {"name": "klip-api-1234-abcde", "uid": "abcde", "node": "platform-01"}
    assert pods == before


def test_stale_prepared_intent_cannot_delete_and_completed_operation_is_reused(
    tmp_path, monkeypatch
):
    import cluster
    import management_api_failure as failure

    monkeypatch.setattr(failure, "folder", lambda: tmp_path)
    operation = "123456abcdef"
    root = tmp_path / "failures" / operation
    root.mkdir(parents=True)
    path = root / "api.json"
    record = {
        "stage": "prepared",
        "image": "pinned-image",
        "targets": ["platform-01", "platform-02"],
        "namespace_uid": "namespace-one",
        "deployment_uid": "old-deployment",
        "target": {"uid": "abcde"},
    }
    path.write_text(json.dumps(record))
    monkeypatch.setattr(failure, "observe", allocation)
    mutate = Mock()
    monkeypatch.setattr(cluster, "delete_owned_resource", mutate)
    monkeypatch.setattr(
        cluster, "kubectl", lambda *a: json.dumps({"metadata": {"uid": "namespace-one"}})
    )
    with pytest.raises(ValueError, match="identity changed"):
        failure.run(operation)
    mutate.assert_not_called()
    record["stage"] = "replaced"
    path.write_text(json.dumps(record))
    assert failure.run(operation) == record
    mutate.assert_not_called()
