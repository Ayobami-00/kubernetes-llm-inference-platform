"""Exercise the full inspection sequence with explicit synthetic external observations."""

import base64
import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import cluster
import pytest
from platform_contracts import HostTask, HostTaskObservation
from platform_management.host_executor import gpu_telemetry, inspection, inspection_probes
from platform_management.jobs.real_worker import RealWorkerWorkflow
from platform_management.services.common import AppError

UID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    task = HostTask(
        id="host-" + "a" * 24,
        action="inspect",
        provider_instance_id="b" * 32,
        node_name="gpu-" + "b" * 32,
        node_uid=UID,
        public_ip="8.8.8.8",
        private_ip="192.168.1.2",
        wireguard_ip="10.200.0.67",
        registry_revision=13,
        deadline=datetime.now(UTC) + timedelta(minutes=20),
        previous_task_id="host-" + "c" * 24,
        trusted_host_key="ssh-ed25519 "
        + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"x" * 32).decode(),
    )
    cpu = ["cp-01", "cp-02", "cp-03", "platform-01", "platform-02", "platform-03"]
    peers = {name: SimpleNamespace(instance_id=name) for name in cpu}
    peers[task.node_name] = SimpleNamespace(instance_id=task.provider_instance_id)
    registry = SimpleNamespace(peers=peers, revision=13)
    fields = {
        "ansible_host": str(task.public_ip),
        "klip_node": {
            "instance_id": task.provider_instance_id,
            "wireguard_ip": str(task.wireguard_ip),
            "runtime_profile": "nvidia-operator",
            "provider": "lambda",
        },
    }
    nodes = [
        {"metadata": {"name": name, "uid": name + "-uid"}, "spec": {"providerID": "aws:///" + name}}
        for name in cpu
    ]
    gpu = {
        "metadata": {"name": task.node_name, "uid": UID, "resourceVersion": "1", "labels": {}},
        "spec": {
            "providerID": "lambda:///" + task.provider_instance_id,
            "taints": [{"key": "klip.dev/bootstrap", "value": "true", "effect": "NoSchedule"}],
        },
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "addresses": [{"type": "InternalIP", "address": str(task.wireguard_ip)}],
            "capacity": {"nvidia.com/gpu": "1"},
            "allocatable": {"nvidia.com/gpu": "1"},
        },
    }
    nodes.append(gpu)
    state = SimpleNamespace(
        task=task,
        root=tmp_path,
        nodes=nodes,
        promotions=0,
        node_reads=0,
        fault=None,
        registry=registry,
        inputs={"cpu_peers": {n: {} for n in cpu}, "kube_system_uid": UID},
    )
    directory = tmp_path / "executions" / task.id
    directory.mkdir(parents=True)
    (directory / "observation.json").write_text(
        HostTaskObservation(
            task_id=task.id,
            input_sha256="a" * 64,
            status="running",
            stage="Synthetic test",
            job_uid="native-synthetic-job",
            observed_at=datetime.now(UTC),
            registry_revision=13,
        ).model_dump_json()
    )

    class Host:
        def __init__(self, _):
            pass

        def agent(self, _):
            if state.fault == "host":
                raise ValueError("Synthetic host failure")
            return {"host": True, "instance_id": task.provider_instance_id}

    def kubectl(_, *args):
        if args[:2] == ("get", "nodes"):
            state.node_reads += 1
            result = copy.deepcopy(nodes)
            if state.fault == "node-changed" and state.node_reads > 1:
                result[-1]["metadata"]["uid"] = "replacement"
            return json.dumps({"items": result})
        if args[:2] == ("get", "node"):
            return json.dumps(gpu)
        if args[:2] == ("patch", "node"):
            patch = json.loads(args[args.index("-p") + 1])
            promotion = any(
                p.get("value") == "true" and inspection.READY_LABEL.replace("/", "~1") in p["path"]
                for p in patch
            )
            if promotion:
                assert (directory / "inspection-proof.json").exists()
                assert state.fault is None
                state.promotions += 1
            for p in patch:
                if p["op"] == "add":
                    gpu["metadata"]["labels"][p["path"].split("/", 3)[3].replace("~1", "/")] = p[
                        "value"
                    ]
                elif p["op"] == "replace":
                    gpu["spec"]["taints"] = p["value"]
            return json.dumps(gpu)
        raise AssertionError(args)

    calls = {"approval": 0, "operator": 0}

    def approval(*_):
        calls["approval"] += 1
        if state.fault == "approval":
            raise ValueError("Synthetic missing approval")
        return {
            "review": "changed"
            if state.fault == "approval-changed" and calls["approval"] > 1
            else "fixed",
            "operator": {"policy": "fixed"},
        }

    def operator(_):
        calls["operator"] += 1
        if state.fault == "operator":
            raise ValueError("Synthetic operator error")
        return {
            "policy": "changed"
            if state.fault == "operator-changed" and calls["operator"] > 1
            else "fixed"
        }

    def stage(name, value):
        def check(*args):
            if state.fault == name:
                raise ValueError("Synthetic " + name + " failure")
            return copy.deepcopy(value)

        return check

    monkeypatch.setattr(cluster, "assert_peer_writer", lambda: None)
    monkeypatch.setattr(cluster, "load_hosts", lambda: {task.node_name: fields})
    monkeypatch.setattr(cluster, "Host", Host)
    monkeypatch.setattr(inspection, "checked_peer", lambda *_: None)
    monkeypatch.setattr(inspection, "kubectl", kubectl)
    monkeypatch.setattr(inspection, "approval", approval)
    monkeypatch.setattr(inspection, "operator_configuration", operator)
    monkeypatch.setattr(inspection, "wireguard", stage("wireguard", {"direct_peers": cpu}))
    monkeypatch.setattr(
        inspection,
        "operands",
        stage(
            "operands",
            [
                {
                    "app": "nvidia-dcgm-exporter",
                    "name": "dcgm",
                    "node": task.node_name,
                    "uid": "dcgm-uid",
                }
            ],
        ),
    )
    monkeypatch.setattr(inspection, "cilium", stage("cilium", {"status": "synthetic-ready"}))
    monkeypatch.setattr(
        inspection_probes,
        "execute",
        stage(
            "probes",
            {
                "hardware": {
                    "index": 0,
                    "uuid": "GPU-" + UID,
                    "name": "NVIDIA A10",
                    "memory_total_mib": 23028,
                    "driver_version": "595.91.07",
                },
                "cleanup": [],
            },
        ),
    )
    monkeypatch.setattr(
        gpu_telemetry, "observe", stage("telemetry", {"replicas": ["synthetic-0", "synthetic-1"]})
    )
    return state


def test_full_inspection_persists_proof_before_promoting_and_emits_actual_facts(pipeline):
    p = pipeline
    result = inspection.perform_inspection(p.root, p.task, p.inputs, p.registry, None)
    assert p.promotions == 1 and result["readiness"]["gpu_runtime"]
    assert result["gpu_observation"]["devices"][0]["memory_total_mib"] == 23028
    assert not p.nodes[-1]["spec"]["taints"]
    assert (p.root / "executions" / p.task.id / "inspection-evidence.json").exists()


@pytest.mark.parametrize(
    "fault",
    [
        "approval",
        "host",
        "operator",
        "wireguard",
        "operands",
        "probes",
        "telemetry",
        "cilium",
        "operator-changed",
        "node-changed",
        "approval-changed",
    ],
)
def test_any_failed_proof_keeps_bootstrap_quarantined(pipeline, fault):
    p = pipeline
    p.fault = fault
    with pytest.raises(ValueError):
        inspection.perform_inspection(p.root, p.task, p.inputs, p.registry, None)
    assert p.promotions == 0
    assert any(t["key"] == "klip.dev/bootstrap" for t in p.nodes[-1]["spec"]["taints"])


def test_durable_proof_write_failure_never_promotes(pipeline, monkeypatch):
    original = inspection.write_private

    def write(path, value):
        if path.name == "inspection-proof.json":
            raise OSError("Synthetic retained-storage failure")
        return original(path, value)

    monkeypatch.setattr(inspection, "write_private", write)
    p = pipeline
    with pytest.raises(OSError):
        inspection.perform_inspection(p.root, p.task, p.inputs, p.registry, None)
    assert p.promotions == 0
    assert any(t["key"] == "klip.dev/bootstrap" for t in p.nodes[-1]["spec"]["taints"])


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"gpu_inspection_configured": False},
        {"gpu_inspection_configured": True},
        {"gpu_inspection_configured": True, "gpu_runtime_review_sha256": "wrong"},
    ],
)
def test_new_paid_dispatch_requires_configured_inspection(metadata):
    flow = object.__new__(RealWorkerWorkflow)
    flow.metadata = lambda: metadata
    with pytest.raises(AppError) as caught:
        flow.inspection_metadata()
    assert caught.value.code == "gpu_inspection_unconfigured"
