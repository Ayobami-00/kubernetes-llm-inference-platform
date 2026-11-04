"""Typed executor ownership, privilege boundaries and lost-attempt behavior."""

import base64
import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from platform_contracts import HostTask, HostTaskObservation
from platform_management.host_executor.controller import HostController, canonical
from platform_management.providers.base import TransientProviderError
from platform_management.services.common import AppError

IMAGE = "127811062476.dkr.ecr.eu-west-2.amazonaws.com/klip/management-backend@sha256:" + "a" * 64


def task(identity="1", **changes):
    document = {
        "id": "host-" + identity * 24,
        "action": "prepare",
        "provider_instance_id": "a" * 32,
        "node_name": "gpu-" + "a" * 32,
        "public_ip": "8.8.8.8",
        "private_ip": "192.168.10.1",
        "wireguard_ip": "10.200.0.67",
        "trusted_host_key": "ssh-ed25519 "
        + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"x" * 32).decode(),
        "registry_revision": 1,
        "deadline": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    }
    return HostTask.model_validate({**document, **changes})


class Cluster:
    def __init__(self):
        self.jobs, self.posts = {}, []
        self.lost_response, self.fail_all = False, False

    def job_path(self):
        return "apis/batch/v1/namespaces/platform-ops/jobs"

    def request(self, method, path, body, **_):
        assert not self.fail_all
        assert method == "POST" and path == self.job_path()
        self.posts.append(copy.deepcopy(body))
        job = copy.deepcopy(body)
        job["metadata"]["uid"] = "job-" + body["metadata"]["name"]
        self.jobs[job["metadata"]["name"]] = job
        if self.lost_response:
            raise TransientProviderError("Synthetic lost response after Job creation")
        return job

    def job(self, identity):
        assert not self.fail_all
        return self.jobs.get(identity)


def terminal(controller, document, **changes):
    result = HostTaskObservation.model_validate(
        {
            "task_id": document.id,
            "input_sha256": hashlib.sha256(canonical(document)).hexdigest(),
            "job_uid": "job-" + document.id,
            "status": "completed",
            "stage": "Host prepared",
            "observed_at": datetime.now(UTC).isoformat(),
            "registry_revision": 1,
            "host_ready": True,
            **changes,
        }
    )
    (controller.directory(document.id) / "observation.json").write_text(result.model_dump_json())
    return result


@pytest.mark.parametrize(
    "changes",
    [
        {"node_name": "cp-01"},
        {"node_name": "platform-01"},
        {"node_name": "../escape"},
        {"wireguard_ip": "10.200.0.64"},
        {"wireguard_ip": "10.200.0.255"},
        {"private_ip": "10.40.0.11"},
        {"private_ip": "10.244.2.3"},
        {"public_ip": "169.254.169.254"},
        {"registry_revision": True},
        {"command": ["arbitrary"]},
        {"access_ref": "/private/key"},
        {"action": "remove"},
        {"trusted_host_key": "PRIVATE KEY"},
    ],
)
def test_contract_rejects_unscoped_targets_and_command_or_credential_injection(changes):
    with pytest.raises(ValueError):
        task(**changes)


def test_fixed_job_has_no_api_token_or_host_mounts(tmp_path):
    controller = HostController(tmp_path, IMAGE, Cluster())
    manifest = controller.manifest(task())
    spec, pod = manifest["spec"], manifest["spec"]["template"]["spec"]
    assert spec["backoffLimit"] == 0 and 0 < spec["activeDeadlineSeconds"] <= 1200
    assert pod["automountServiceAccountToken"] is False and pod["restartPolicy"] == "Never"
    assert pod["serviceAccountName"] == "klip-host-job"
    assert pod["securityContext"]["runAsUser"] == 20002
    assert not any("hostPath" in v for v in pod["volumes"])
    tokens = [v for v in pod["volumes"] if "projected" in v]
    assert len(tokens) == 1
    assert (
        tokens[0]["projected"]["sources"][0]["serviceAccountToken"]["audience"]
        == "sts.amazonaws.com"
    )
    assert pod["containers"][0]["command"][:3] == [
        "python",
        "-m",
        "platform_management.host_executor.worker",
    ]
    assert "sh" not in pod["containers"][0]["command"]


def test_terminal_and_repeated_keys_never_need_new_api_access_or_credentials(tmp_path):
    cluster, document = Cluster(), task()
    controller = HostController(tmp_path, IMAGE, cluster)
    first = controller.launch(document)
    assert first.status == "running"
    assert controller.launch(document).job_uid == first.job_uid
    done = terminal(controller, document)
    cluster.fail_all = True
    assert controller.launch(document) == done and len(cluster.posts) == 1
    with pytest.raises(ValueError, match="Immutable"):
        controller.launch(document.model_copy(update={"drain_timeout_s": 30}))


def test_ambiguous_post_is_observed_without_second_post_and_uid_is_frozen(tmp_path):
    cluster, document = Cluster(), task()
    cluster.lost_response = True
    controller = HostController(tmp_path, IMAGE, cluster)
    with pytest.raises(TransientProviderError):
        controller.launch(document)
    assert controller.launch(document).status == "running" and len(cluster.posts) == 1
    assert (controller.directory(document.id) / "job-uid").is_file()
    cluster.jobs[document.id]["metadata"]["uid"] = "replacement-uid"
    with pytest.raises(ValueError):
        controller.observe(document.id)


def test_lost_job_fails_without_recreating_execution(tmp_path):
    cluster, document = Cluster(), task()
    controller = HostController(tmp_path, IMAGE, cluster)
    controller.launch(document)
    cluster.jobs.clear()
    result = controller.launch(document)
    assert result.status == "failed" and result.cleanup_pending
    assert result.error.code == "host_job_lost" and len(cluster.posts) == 1


def test_inflight_or_failed_task_blocks_another_host_mutation(tmp_path):
    cluster, first, second = Cluster(), task("1"), task("2")
    controller = HostController(tmp_path, IMAGE, cluster)
    controller.launch(first)
    with pytest.raises(AppError, match="reconciliation"):
        controller.launch(second)
    terminal(
        controller,
        first,
        status="failed",
        error={"code": "bootstrap_failed", "message": "Synthetic failure"},
        cleanup_pending=True,
    )
    with pytest.raises(AppError):
        controller.launch(second)
    assert len(cluster.posts) == 1


def test_followup_cannot_change_trusted_instance_or_key(tmp_path):
    cluster, first = Cluster(), task()
    controller = HostController(tmp_path, IMAGE, cluster)
    controller.launch(first)
    terminal(controller, first)
    changed = task("2", action="join", previous_task_id=first.id, provider_instance_id="b" * 32)
    with pytest.raises(AppError, match="task chain"):
        controller.launch(changed)
    assert len(cluster.posts) == 1


def test_cpu_endpoint_is_rejected_before_job_creation(tmp_path):
    cluster, document = Cluster(), task()
    controller = HostController(tmp_path, IMAGE, cluster, protected_ips=[str(document.public_ip)])
    with pytest.raises(AppError, match="persistent CPU"):
        controller.launch(document)
    assert not cluster.posts


@pytest.mark.parametrize("change", ["init", "privileged", "image", "command"])
def test_job_configuration_drift_is_rejected(tmp_path, change):
    cluster, document = Cluster(), task()
    controller = HostController(tmp_path, IMAGE, cluster)
    controller.launch(document)
    pod = cluster.jobs[document.id]["spec"]["template"]["spec"]
    if change == "init":
        pod["initContainers"] = [{"name": "unexpected"}]
    elif change == "privileged":
        pod["containers"][0]["securityContext"]["privileged"] = True
    else:
        pod["containers"][0][change] = "changed"
    with pytest.raises((AppError, ValueError)):
        controller.observe(document.id)


def test_ready_claim_needs_gpu_checks_and_consistent_node_identity(tmp_path):
    controller, document = HostController(tmp_path, IMAGE, Cluster()), task()
    controller.launch(document)
    with pytest.raises(ValueError, match="GPU readiness"):
        terminal(controller, document, gpu_runtime_ready=True)
    valid = terminal(controller, document)
    raw = json.loads(valid.model_dump_json())
    raw["node_name"], raw["node_uid"] = "another-node", "another-uid"
    (controller.directory(document.id) / "observation.json").write_text(json.dumps(raw))
    with pytest.raises(AppError, match="durable task"):
        controller.observe(document.id)


def test_worker_has_one_permanent_owner_and_preserves_terminal_artifacts(tmp_path, monkeypatch):
    from platform_management.host_executor.worker import run_task

    document, cluster = task(), Cluster()
    controller = HostController(tmp_path, IMAGE, cluster)
    controller.launch(document)
    (tmp_path / "cluster").mkdir()
    monkeypatch.setenv("POD_UID", "original-pod")
    monkeypatch.setenv("JOB_UID", "job-" + document.id)
    calls = []

    def action(root, supplied):
        calls.append((root, supplied))
        return {
            "status": "completed",
            "provider_instance_id": supplied.provider_instance_id,
            "registry_revision": 1,
            "public_key": base64.b64encode(b"x" * 32).decode(),
            "readiness": {"host": True, "wireguard": False, "kubernetes": False, "cilium": False},
        }

    result = run_task(tmp_path, document.id, action=action)
    assert result.status == "completed" and result.host_ready
    assert result.gpu_runtime_ready is False and result.connectivity_verified is False
    assert controller.observe(document.id) == result
    assert run_task(tmp_path, document.id, action=action) == result and len(calls) == 1
    monkeypatch.setenv("POD_UID", "different-pod")
    with pytest.raises(ValueError, match="Immutable"):
        run_task(tmp_path, document.id, action=action)
    assert controller.observe(document.id) == result and len(calls) == 1


def test_worker_failure_keeps_private_diagnostics_without_claiming_readiness(tmp_path, monkeypatch):
    from platform_management.host_executor.worker import run_task

    document, cluster = task(), Cluster()
    controller = HostController(tmp_path, IMAGE, cluster)
    controller.launch(document)
    (tmp_path / "cluster").mkdir()
    monkeypatch.setenv("POD_UID", "original-pod")
    monkeypatch.setenv("JOB_UID", "job-" + document.id)

    def fail(*_):
        raise RuntimeError("Synthetic executor diagnostic")

    result = run_task(tmp_path, document.id, action=fail)
    assert result.status == "failed" and result.cleanup_pending and not result.gpu_runtime_ready
    assert "Synthetic executor diagnostic" not in result.model_dump_json()
    private = controller.directory(document.id) / "private-error.json"
    assert "Synthetic executor diagnostic" in private.read_text()
    assert controller.observe(document.id) == result


def test_worker_rejects_changed_inputs_before_privileged_action(tmp_path, monkeypatch):
    from platform_management.host_executor.worker import run_task

    document, cluster = task(), Cluster()
    controller = HostController(tmp_path, IMAGE, cluster)
    controller.launch(document)
    changed = document.model_copy(update={"drain_timeout_s": 15})
    (controller.directory(document.id) / "task.json").write_text(changed.model_dump_json())
    monkeypatch.setenv("POD_UID", "original-pod")
    monkeypatch.setenv("JOB_UID", "job-" + document.id)
    with pytest.raises(ValueError, match="Frozen host input"):
        run_task(tmp_path, document.id, action=lambda *_: pytest.fail("Must not execute"))


def test_ssh_control_sockets_can_use_local_scratch_instead_of_nfs(tmp_path, monkeypatch):
    import tempfile

    from cluster import Host

    # NFS is appropriate for durable receipts, but not Unix-domain SSH sockets.
    with tempfile.TemporaryDirectory(prefix="klip-ssh-", dir="/tmp") as local:
        monkeypatch.setenv("KLIP_SSH_SOCKET_HOME", local)
        host = Host(
            {
                "ansible_host": "8.8.8.8",
                "ansible_user": "ubuntu",
                "ansible_ssh_private_key_file": "/synthetic/key",
            }
        )
        arguments = host.ssh_arguments()
        assert any(a.startswith("ControlPath=" + local) for a in arguments)
    monkeypatch.setenv("KLIP_SSH_SOCKET_HOME", "relative-path")
    with pytest.raises(ValueError, match="absolute"):
        host.ssh_arguments()
