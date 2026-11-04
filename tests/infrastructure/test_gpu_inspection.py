"""GPU admission requires actual identities and proof, never generic Node Ready."""

import base64
import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta

import gpu_operator
import pytest
from platform_contracts import GPUCapacityObservation, HostTask, HostTaskObservation
from platform_contracts.worker_groups import WorkerMember
from platform_management.host_executor import (
    gpu_approval,
    gpu_telemetry,
    inspection,
    inspection_probes,
)

NODE_UID = "11111111-2222-3333-4444-555555555555"
GPU_UUID = "GPU-11111111-2222-3333-4444-555555555555"


@pytest.fixture
def task():
    return HostTask(
        id="host-" + "a" * 24,
        action="inspect",
        provider_instance_id="b" * 32,
        node_name="gpu-" + "b" * 32,
        node_uid=NODE_UID,
        public_ip="8.8.8.8",
        private_ip="192.168.1.2",
        wireguard_ip="10.200.0.67",
        trusted_host_key="ssh-ed25519 "
        + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"x" * 32).decode(),
        registry_revision=13,
        deadline=datetime.now(UTC) + timedelta(minutes=20),
        previous_task_id="host-" + "c" * 24,
    )


def node(task):
    return {
        "metadata": {
            "name": task.node_name,
            "uid": task.node_uid,
            "resourceVersion": "10",
            "labels": {"kubernetes.io/hostname": task.node_name},
        },
        "spec": {
            "providerID": "lambda:///" + task.provider_instance_id,
            "taints": [
                {"key": "klip.dev/bootstrap", "value": "true", "effect": "NoSchedule"},
                {"key": "nvidia.com/gpu", "effect": "NoSchedule"},
            ],
        },
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "addresses": [{"type": "InternalIP", "address": str(task.wireguard_ip)}],
            "capacity": {"nvidia.com/gpu": "1"},
            "allocatable": {"nvidia.com/gpu": "1"},
        },
    }


@pytest.mark.parametrize(
    "problem",
    [
        "uid",
        "provider",
        "address",
        "cordon",
        "not-ready",
        "cpu",
        "missing-taint",
        "extra-taint",
        "ready-label",
        "admission-identity",
        "gpu-count",
    ],
)
def test_invalid_node_cannot_enter_gpu_inspection(task, problem):
    value = node(task)
    if problem == "uid":
        value["metadata"]["uid"] = "another"
    elif problem == "provider":
        value["spec"]["providerID"] = "lambda:///different"
    elif problem == "address":
        value["status"]["addresses"][0]["address"] = "10.200.0.68"
    elif problem == "cordon":
        value["spec"]["unschedulable"] = True
    elif problem == "not-ready":
        value["status"]["conditions"][0]["status"] = "False"
    elif problem == "cpu":
        value["metadata"]["labels"]["klip.dev.node-restriction.kubernetes.io/platform"] = "true"
    elif problem == "missing-taint":
        value["spec"]["taints"] = []
    elif problem == "extra-taint":
        value["spec"]["taints"].append(
            {"key": "klip.dev/bootstrap", "value": "other", "effect": "NoExecute"}
        )
    elif problem == "ready-label":
        value["metadata"]["labels"][inspection.READY_LABEL] = "true"
    elif problem == "admission-identity":
        value["metadata"]["labels"][inspection.UID_LABEL] = "another"
    else:
        value["status"]["allocatable"]["nvidia.com/gpu"] = "2"
    with pytest.raises(ValueError):
        inspection.checked_node(task, value, require_gpu=True)


def test_admission_and_promotion_are_uid_version_guarded_and_preserve_other_taints(task):
    current, patches = node(task), []

    class CP:
        def run(self, args):
            patch = json.loads(args[args.index("-p") + 1])
            patches.append(patch)
            assert patch[:2] == [
                {"op": "test", "path": "/metadata/uid", "value": task.node_uid},
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": current["metadata"]["resourceVersion"],
                },
            ]
            for operation in patch:
                if operation["op"] == "add":
                    key = operation["path"].split("/", 3)[3].replace("~1", "/").replace("~0", "~")
                    current["metadata"]["labels"][key] = operation["value"]
                elif operation["op"] == "replace":
                    current["spec"]["taints"] = operation["value"]
            current["metadata"]["resourceVersion"] = "11"
            return json.dumps(current)

    admitted = inspection.patch_node(CP(), task, copy.deepcopy(current))
    assert inspection.READY_LABEL not in admitted["metadata"]["labels"]
    assert any(t["key"] == "klip.dev/bootstrap" for t in admitted["spec"]["taints"])
    promoted = inspection.patch_node(CP(), task, copy.deepcopy(current), promote=True)
    assert promoted["spec"]["taints"] == [{"key": "nvidia.com/gpu", "effect": "NoSchedule"}]
    assert promoted["metadata"]["labels"][inspection.READY_LABEL] == "true"
    assert promoted["metadata"]["labels"][inspection.WORKER_LABEL] == "latency"


def test_probe_manifests_are_bounded_nonretrying_and_gpu_uid_bound(task):
    documents = inspection_probes.manifests(
        task, ["cp-01", "cp-02", "cp-03", "platform-01", "platform-02", "platform-03"]
    )
    assert len(documents) == 9
    for document in documents:
        if document["kind"] != "Pod":
            continue
        spec = document["spec"]
        assert spec["restartPolicy"] == "Never" and spec["automountServiceAccountToken"] is False
        assert 30 <= spec["activeDeadlineSeconds"] <= 300
        assert spec["securityContext"]["runAsNonRoot"]
        assert not any(k in spec for k in ("hostNetwork", "hostPID", "volumes"))
        assert all(
            c["securityContext"]["capabilities"]["drop"] == ["ALL"] for c in spec["containers"]
        )
        if "nodeName" not in spec:
            assert spec["nodeSelector"][inspection.UID_LABEL] == task.node_uid
    cuda = next(d for d in documents if d["metadata"]["namespace"] == "gpu-system")
    assert cuda["spec"]["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == "1"
    assert cuda["spec"]["initContainers"][0]["resources"]["limits"]["nvidia.com/gpu"] == "1"


@pytest.mark.parametrize("change", ["image", "args", "security", "owner"])
def test_mutated_or_foreign_probe_cannot_be_adopted(task, change):
    document = inspection_probes.pod(task, "gpu", task.node_name)
    observed = copy.deepcopy(document)
    observed["metadata"]["uid"] = NODE_UID
    if change == "image":
        observed["spec"]["containers"][0]["image"] = "unreviewed:latest"
    elif change == "args":
        observed["spec"]["containers"][0]["args"] = ["arbitrary"]
    elif change == "security":
        observed["spec"]["automountServiceAccountToken"] = True
    else:
        observed["metadata"]["annotations"] = {}
    with pytest.raises(ValueError):
        inspection_probes.owned_probe(document, observed)


@pytest.mark.parametrize(
    "name,memory,driver",
    [
        ("NVIDIA A100", 23028, "595.91.07"),
        ("NVIDIA A10", 1024, "595.91.07"),
        ("NVIDIA A10", 23028, "580.0"),
    ],
)
def test_hardware_description_cannot_substitute_another_gpu_or_driver(name, memory, driver):
    with pytest.raises(ValueError):
        inspection_probes.hardware(f"0, {GPU_UUID}, {name}, {memory}, {driver}")


def test_actual_usable_memory_is_preserved_separately_from_advertised_capacity():
    observed = inspection_probes.hardware(f"0, {GPU_UUID}, NVIDIA A10, 23028, 595.91.07")
    capacity = GPUCapacityObservation(
        observed_at=datetime.now(UTC), devices=[observed], allocatable_gpu_count=1
    )
    assert capacity.devices[0].memory_total_mib == 23028
    with pytest.raises(ValueError):
        GPUCapacityObservation(
            observed_at=datetime.now(UTC), devices=[observed, observed], allocatable_gpu_count=2
        )
    with pytest.raises(ValueError):
        GPUCapacityObservation(
            observed_at=datetime.now(UTC), devices=[observed], allocatable_gpu_count=2
        )


def test_missing_and_stale_dcgm_samples_never_become_zero():
    def result(values):
        return {"status": "success", "data": {"resultType": "vector", "result": values}}

    assert gpu_telemetry.samples(result([]), now=100) == []
    assert gpu_telemetry.samples(result([{"value": [100, "0"]}]), now=100) == []
    assert gpu_telemetry.samples(result([{"value": [100, "NaN"]}]), now=100) == []
    assert gpu_telemetry.samples(result([{"value": [100, "99"]}]), now=100) == [99]


def test_missing_runtime_approval_is_unconfigured_without_revealing_paths(tmp_path):
    assert gpu_approval.configured(tmp_path, NODE_UID) == {
        "gpu_inspection_configured": False,
        "gpu_runtime_review_sha256": None,
    }


@pytest.mark.parametrize(
    "change",
    [
        None,
        "expired",
        "cluster",
        "lock",
        "images",
        "kernel-artifacts",
        "missing-assessment",
        "assessment-drift",
    ],
)
def test_runtime_approval_binds_exact_pins_cluster_and_expiry(tmp_path, monkeypatch, change):
    now = datetime.now(UTC)
    review = tmp_path / "runtime-review.json"
    data = {
        "profile": "single-a10",
        "reviewed_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(days=1)).isoformat(),
        "configuration": {},
        "images": {},
    }
    review.write_text(json.dumps(data))
    monkeypatch.setattr(gpu_approval, "REVIEW", review)
    value = {
        "schema_version": "klip-gpu-runtime-approval.v1",
        "profile": "single-a10",
        "kube_system_uid": NODE_UID,
        "review_sha256": hashlib.sha256(review.read_bytes()).hexdigest(),
        "operator_lock_sha256": hashlib.sha256(gpu_approval.LOCK.read_bytes()).hexdigest(),
        "host_lock_sha256": hashlib.sha256(gpu_approval.HOST_LOCK.read_bytes()).hexdigest(),
        "base_artifacts_sha256": hashlib.sha256(gpu_approval.BASE_LOCK.read_bytes()).hexdigest(),
        "assessment_sha256": "a" * 64,
        "images": gpu_operator.settings()["images"],
        "operator": {
            "deployment_uid": NODE_UID,
            "policy_uid": NODE_UID,
            "policy_spec_sha256": "b" * 64,
        },
        **{k: v for k, v in data.items() if k not in {"configuration", "images"}},
    }
    if change == "expired":
        value["expires_at"] = (now - timedelta(seconds=1)).isoformat()
    elif change == "cluster":
        value["kube_system_uid"] = "other"
    elif change == "lock":
        value["operator_lock_sha256"] = "c" * 64
    elif change == "images":
        value["images"] = {}
    elif change == "kernel-artifacts":
        value["base_artifacts_sha256"] = "c" * 64
    (tmp_path / "cluster").mkdir()
    assessment = {
        "schema_version": "klip-gpu-runtime-assessment.v1",
        "status": "conditionally-reviewed-single-a10-bootstrap-only",
        "profile": "single-a10",
        "configuration": {},
        "images": {},
        "review_sha256": value["review_sha256"],
        "expires_at": data["expires_at"],
    }
    body = json.dumps(assessment).encode()
    value["assessment_sha256"] = hashlib.sha256(body).hexdigest()
    if change != "missing-assessment":
        (tmp_path / "cluster/gpu-runtime-assessment.json").write_bytes(
            body + b" " if change == "assessment-drift" else body
        )
    (tmp_path / "cluster/gpu-runtime-approval.json").write_text(json.dumps(value))
    assert gpu_approval.configured(tmp_path, NODE_UID)["gpu_inspection_configured"] is (
        change is None
    )


def test_new_optional_gpu_field_preserves_historical_receipt_and_member_hashes(task):
    observation = HostTaskObservation(
        task_id=task.id,
        input_sha256="a" * 64,
        status="failed",
        stage="Synthetic history",
        observed_at=datetime.now(UTC),
        registry_revision=1,
        error={"code": "test", "message": "Synthetic terminal history"},
    )
    original = observation.model_dump(mode="json")
    assert "gpu_observation" not in original
    assert HostTaskObservation.model_validate(original).model_dump(mode="json") == original
    member = {
        "provider_instance_id": "test",
        "phase": "allocated",
        "node_name": None,
        "node_uid": None,
        "error": None,
    }
    assert WorkerMember.model_validate(member).model_dump(mode="json") == member


def test_operand_runtime_identity_is_checked(task, monkeypatch):
    pods = []
    for app, reference in inspection.PRIMARY_IMAGES.items():
        pinned = gpu_operator.image(reference)
        pods.append(
            {
                "metadata": {
                    "name": app + "-test",
                    "uid": app,
                    "labels": {"app": app},
                    "ownerReferences": [{"kind": "DaemonSet", "controller": True}],
                },
                "spec": {
                    "nodeName": task.node_name,
                    "containers": [{"name": app, "image": pinned}],
                },
                "status": {"containerStatuses": [{"name": app, "ready": True, "imageID": pinned}]},
            }
        )
    monkeypatch.setattr(inspection, "kubectl", lambda *a: json.dumps({"items": pods}))
    assert len(inspection.operands(None, task)) == 6
    pods[0]["status"]["containerStatuses"][0]["imageID"] = "sha256:wrong"
    with pytest.raises(ValueError, match="runtime image"):
        inspection.operands(None, task)
