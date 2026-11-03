"""Failure-focused checks for storage, mesh changes, recovery and worker identity."""

import base64
import hashlib
import io
import json
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import cluster
import cluster_acceptance
import node_agent
import pytest
import yaml
from cluster_backups import backup_fresh, validate_bundle
from cluster_config import firewall, kubeadm_documents, node_configuration
from cluster_models import PeerRegistry, WorkerRequest, assert_node_identity
from cluster_verification import ready_node
from node_agent import cluster_state, disk_decision, gateway_decision, validate_pod_mtu


@pytest.mark.parametrize("existing", [False, True])
def test_verification_cleans_only_its_own_fixture_on_failure(monkeypatch, existing):
    calls, applied = [], []

    def kubectl(*arguments):
        calls.append(arguments)
        return '{"metadata": {"name": "klip-acceptance"}}' if existing else ""

    def fail():
        raise RuntimeError("Network check failed")

    monkeypatch.setattr(cluster, "kubectl", kubectl)
    monkeypatch.setattr(cluster, "_verify_cluster_state", fail)
    monkeypatch.setattr(cluster_acceptance, "remote_manifest", applied.append)
    with pytest.raises(RuntimeError, match="Network check"):
        cluster.verify()
    assert bool(applied) is not existing
    assert any(call[0] == "delete" for call in calls) is not existing


def test_ssh_reuse_is_scoped_to_authenticated_access_settings(monkeypatch):
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="klip-") as folder:
        _check_ssh_reuse(Path(folder), monkeypatch)


def _check_ssh_reuse(tmp_path, monkeypatch):
    monkeypatch.setattr(cluster, "state_home", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(cluster, "checked", lambda arguments, **kwargs: calls.append(arguments))
    fields = {
        "ansible_host": "i-123",
        "ansible_user": "ubuntu",
        "ansible_ssh_private_key_file": "/operator/key",
        "ansible_ssh_common_args": "-o UserKnownHostsFile=/operator/pins",
    }
    cluster.Host(fields).run(["true"])
    cluster.Host(fields).run(["true"])
    cluster.Host({**fields, "ansible_ssh_common_args": "-o UserKnownHostsFile=/new/pins"}).run(
        ["true"]
    )
    paths = [next(arg for arg in call if arg.startswith("ControlPath=")) for call in calls]
    assert paths[0] == paths[1] and paths[0] != paths[2]
    assert "StrictHostKeyChecking=yes" in calls[0]
    assert "ServerAliveCountMax=2" in calls[0]
    assert (tmp_path / "ssh").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "paths,data", [({"admin.conf"}, False), (set(), True), ({"pki/ca.crt"}, True)]
)
def test_partial_bootstrap_never_initializes(paths, data):
    with pytest.raises(ValueError, match="Partial"):
        cluster_state(paths, data)


def test_healthy_bootstrap_is_recognized_without_reset():
    assert cluster_state(set(), False) == "uninitialized"
    assert cluster_state({"manifests/.kubelet-keep"}, False) == "uninitialized"
    assert (
        cluster_state({"admin.conf", "pki/ca.crt", "manifests/kube-apiserver.yaml"}, True)
        == "initialized"
    )


def test_package_marker_does_not_hide_partial_bootstrap_or_data():
    for paths, data in (
        ({"manifests/.kubelet-keep", "pki/ca.key"}, False),
        ({"manifests/.kubelet-keep"}, True),
    ):
        with pytest.raises(ValueError, match="Partial"):
            cluster_state(paths, data)


def test_pod_mtu_checks_the_effective_route_not_only_the_interface():
    assert validate_pod_mtu([{"mtu": 1420}], [{"dst": "default", "metrics": [{"mtu": 1370}]}]) == {
        "pod_interface_mtu": 1420,
        "pod_route_mtu": 1370,
    }
    for routes in ([], [{"dst": "default"}], [{"dst": "default", "metrics": [{"mtu": 1420}]}]):
        with pytest.raises(ValueError, match="route"):
            validate_pod_mtu([{"mtu": 1420}], routes)


def test_mesh_renumber_removes_old_primary_before_reestablishing_assignment(monkeypatch, registry):
    calls = []

    def command(*args, **kwargs):
        calls.append(args)
        if args == ("ip", "-j", "address", "show", "dev", "wg0"):
            return json.dumps(
                [
                    {
                        "addr_info": [
                            {"family": "inet", "local": "10.200.0.2", "prefixlen": 24},
                            {"family": "inet", "local": "10.200.0.11", "prefixlen": 24},
                        ]
                    }
                ]
            )
        return ""

    monkeypatch.setattr(node_agent, "command", command)
    monkeypatch.setattr(node_agent, "atomic", lambda *args: None)
    monkeypatch.setattr(Path, "read_text", lambda *args: "example-key")
    monkeypatch.setattr(
        node_agent.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1)
    )
    node_agent.apply_mesh({"name": "cp-01", "api_routed_cidrs": []}, registry, {})
    deletion = ("ip", "address", "del", "10.200.0.2/24", "dev", "wg0")
    assignment = ("ip", "address", "replace", "10.200.0.11/24", "dev", "wg0")
    assert calls.index(deletion) < calls.index(assignment)
    assert sum(call[:3] == ("ip", "address", "del") for call in calls) == 1


@pytest.mark.parametrize(
    "root,candidate,filesystem,signatures",
    [
        ("/dev/nvme0n1", "/dev/nvme0n1", "", ""),
        ("/dev/nvme0n1", "/dev/nvme1n1", "xfs", "xfs"),
        ("/dev/nvme0n1", "/dev/nvme1n1", "", "gpt"),
    ],
)
def test_storage_refuses_root_or_existing_unknown_data(root, candidate, filesystem, signatures):
    with pytest.raises(ValueError):
        disk_decision(root, candidate, filesystem, signatures)


def test_storage_preserves_recognized_filesystem():
    assert disk_decision("root", "etcd", "ext4", "ext4") == "reuse"
    assert disk_decision("root", "etcd", "", "") == "format-empty"


def test_gateway_failure_threshold_cooldown_and_no_failback():
    state = {"active": "cp-01", "switched_at": 0, "failures": 0}
    for now in (100, 105):
        state, changed = gateway_decision(state, {"cp-01": False, "cp-02": True}, now)
        assert not changed
    state, changed = gateway_decision(state, {"cp-01": False, "cp-02": True}, 110)
    assert changed and state["active"] == "cp-02"
    state, changed = gateway_decision(state, {"cp-01": True, "cp-02": True}, 200)
    assert not changed and state["active"] == "cp-02"
    state.update(failures=2)
    held, changed = gateway_decision(state, {"cp-01": True, "cp-02": False}, 115)
    assert not changed and held["active"] == "cp-02"


def test_no_gateway_is_invented_when_all_unhealthy():
    state, changed = gateway_decision({}, {"cp-01": False}, 100)
    assert not changed and "active" not in state


@pytest.fixture
def registry():
    return {
        "revision": 1,
        "peers": {
            f"cp-{index:02d}": {
                "instance_id": f"i-{index}",
                "role": "control_plane",
                "public_ip": f"3.11.66.{index}",
                "private_ip": f"10.40.{index - 1}.10",
                "wireguard_ip": f"10.200.0.{10 + index}",
                "public_key": base64.b64encode(bytes([index]) * 32).decode(),
            }
            for index in range(1, 4)
        },
    }


@pytest.mark.parametrize("field", ["instance_id", "public_key", "wireguard_ip"])
def test_mesh_rejects_duplicate_peer_identity(registry, field):
    registry["peers"]["cp-02"][field] = registry["peers"]["cp-01"][field]
    with pytest.raises(ValueError, match="Duplicate"):
        PeerRegistry.model_validate(registry)


def test_registry_rejects_secrets_and_wrong_cpu_allocation(registry):
    registry["peers"]["cp-01"]["private_key"] = "not-allowed"
    with pytest.raises(ValueError):
        PeerRegistry.model_validate(registry)
    del registry["peers"]["cp-01"]["private_key"]
    registry["peers"]["cp-01"]["wireguard_ip"] = "10.200.0.15"
    with pytest.raises(ValueError, match="immutable"):
        PeerRegistry.model_validate(registry)


@pytest.fixture
def worker():
    return {
        "operation_id": "remove-worker",
        "deadline": datetime.now(UTC) + timedelta(minutes=5),
        "provider": "lambda",
        "provider_instance_id": "lambda-instance-1",
        "node_name": "worker-01",
        "node_uid": "uid-one",
        "public_ip": "8.8.8.8",
        "private_ip": "192.168.2.10",
        "wireguard_ip": "10.200.0.64",
        "access_ref": "credential://klip/worker-01",
        "registry_revision": 1,
    }


def test_worker_removal_rejects_reused_name_and_provider(worker):
    request = WorkerRequest.model_validate(worker)
    node = {
        "metadata": {"name": "worker-01", "uid": "uid-two"},
        "spec": {"providerID": "lambda:///lambda-instance-1"},
    }
    with pytest.raises(ValueError, match="UID"):
        assert_node_identity(request, node)
    node["metadata"]["uid"] = "uid-one"
    node["spec"]["providerID"] = "lambda:///different-instance"
    with pytest.raises(ValueError, match="Provider"):
        assert_node_identity(request, node)


def test_worker_deadline_and_credentials_fail_closed(worker):
    worker["deadline"] = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(ValueError, match="expired"):
        WorkerRequest.model_validate(worker).remaining()
    worker["access_ref"] = "inline-password"
    with pytest.raises(ValueError, match="references"):
        WorkerRequest.model_validate(worker)


def test_configuration_keeps_real_api_and_kubelet_mount_guards(allocation):
    config = node_configuration("cp-01", allocation, allocation["nodes"]["cp-01"])
    documents = kubeadm_documents(config)
    cluster = documents[1]
    assert cluster["controlPlaneEndpoint"] == allocation["api"]["endpoint"].removeprefix("https://")
    assert cluster["etcd"]["local"]["dataDir"] == "/var/lib/etcd/data"
    assert documents[0]["localAPIEndpoint"]["advertiseAddress"] == "10.200.0.11"
    assert documents[0]["bootstrapTokens"] == [{"ttl": "15m", "description": "klip initialization"}]
    assert "10.40.1.10" in cluster["apiServer"]["certSANs"]
    assert "PRIVATE KEY" not in yaml.safe_dump_all(documents)
    rules = firewall(config)
    assert "-F INPUT" not in rules and "-F FORWARD" not in rules
    assert "-d 169.254.169.254/32 -j DROP" in rules
    assert "--dport 6443 -j SNAT --to-source 10.40.0.10" in rules
    assert "--dport 22 -j ACCEPT" not in rules


def test_backups_require_versions_and_fresh_observations():
    now = datetime.now(UTC)
    for receipt in (
        {"captured_at": now.isoformat()},
        {"version_id": "v1", "captured_at": (now - timedelta(days=2)).isoformat()},
    ):
        with pytest.raises(ValueError):
            backup_fresh(receipt, now)


def test_bundle_rejects_tampering_and_path_escape(tmp_path):
    path = tmp_path / "recovery.tar"
    with tarfile.open(path, "w") as archive:
        content = b"data"
        item = tarfile.TarInfo("../escape")
        item.size = len(content)
        archive.addfile(item, io.BytesIO(content))
    with pytest.raises(ValueError, match="Unsafe"):
        validate_bundle(path, "i-1")
    with tarfile.open(path, "w") as archive:
        for name, content in {
            "snapshot.db": b"tampered",
            "metadata.json": json.dumps(
                {
                    "schema_version": "klip-recovery.v1",
                    "cluster_id": "klip",
                    "instance_id": "i-1",
                    "snapshot_sha256": hashlib.sha256(b"original").hexdigest(),
                }
            ).encode(),
            "encryption.yaml": b"x",
            "pki/ca.crt": b"x",
            "pki/ca.key": b"x",
        }.items():
            item = tarfile.TarInfo(name)
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))
    with pytest.raises(ValueError, match="checksum"):
        validate_bundle(path, "i-1")


def test_ready_node_does_not_trust_name_alone():
    node = {
        "spec": {"providerID": "aws:///zone/i-wrong"},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "addresses": [{"type": "InternalIP", "address": "10.200.0.11"}],
        },
    }
    with pytest.raises(ValueError, match="provider"):
        ready_node(node, "i-expected", "10.200.0.11")


def test_stale_and_conflicting_mesh_revisions_are_rejected(registry):
    from node_agent import registry_guard

    digest = registry_guard(registry, {})
    with pytest.raises(ValueError, match="Stale"):
        registry_guard(registry, {"revision": 2})
    with pytest.raises(ValueError, match="Conflicting"):
        registry_guard(registry, {"revision": 1, "digest": "different"})
    assert registry_guard(registry, {"revision": 1, "digest": digest}) == digest


def test_healthy_bootstrap_rerun_never_executes_init_or_join(tmp_path, monkeypatch, allocation):
    import cluster
    import cluster_verification

    hosts = {
        name: {"klip_node": node_configuration(name, allocation, item)}
        for name, item in allocation["nodes"].items()
    }
    monkeypatch.setattr(cluster, "FOLDER", tmp_path)
    monkeypatch.setattr(cluster, "load_hosts", lambda: hosts)
    monkeypatch.setattr(cluster, "kubeconfigs", lambda: None)
    monkeypatch.setattr(cluster_verification, "verify_wireguard", lambda _: None)
    (tmp_path / "encryption.yaml").write_text("already retained")

    class HealthyHost:
        def __init__(self, fields):
            pass

        def agent(self, action):
            if action == "state":
                return {"state": "initialized"}
            return {"members": [1, 2, 3], "health": [{"health": True}] * 3}

        def run(self, *args, **kwargs):
            pytest.fail("Healthy bootstrap must not execute mutable host commands")

    monkeypatch.setattr(cluster, "Host", HealthyHost)
    assert cluster.bootstrap()["members"] == ["cp-01", "cp-02", "cp-03"]


def test_real_backup_encryption_detects_ciphertext_tampering(tmp_path):
    import subprocess

    from common import ROOT

    tools = ROOT / "artifacts/tools/bin"
    if not (tools / "age-keygen").exists():
        pytest.skip("Install locked platform tools for the crypto integration check")
    key, plain, encrypted, restored = (
        tmp_path / item for item in ["key", "plain", "encrypted", "restored"]
    )
    subprocess.run([str(tools / "age-keygen"), "-o", str(key)], capture_output=True, check=True)
    recipient = subprocess.check_output(
        [str(tools / "age-keygen"), "-y", str(key)], text=True
    ).strip()
    plain.write_bytes(b"recovery data fixture")
    subprocess.run(
        [str(tools / "age"), "-r", recipient, "-o", str(encrypted), str(plain)], check=True
    )
    subprocess.run(
        [str(tools / "age"), "-d", "-i", str(key), "-o", str(restored), str(encrypted)], check=True
    )
    assert restored.read_bytes() == plain.read_bytes()
    payload = bytearray(encrypted.read_bytes())
    payload[-1] ^= 1
    encrypted.write_bytes(payload)
    result = subprocess.run(
        [str(tools / "age"), "-d", "-i", str(key), str(encrypted)], capture_output=True
    )
    assert result.returncode != 0


def test_invalid_worker_cli_never_echoes_inline_credentials(tmp_path):
    import os
    import subprocess
    import sys

    from common import ROOT

    request = tmp_path / "bad-request.json"
    request.write_text(json.dumps({"inline_secret": "do-not-echo-this-value"}))
    env = dict(os.environ, KLIP_STATE_HOME=str(tmp_path / "operator"))
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/infrastructure/cluster.py"),
            "worker-join",
            "--request",
            str(request),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "do-not-echo-this-value" not in result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "failed"


def test_kubeadm_endpoint_survives_shared_url_normalization(allocation):
    from inventory import FoundationAllocation

    normalized = FoundationAllocation.model_validate(allocation).model_dump(mode="json")
    assert normalized["api"]["endpoint"].endswith("/")
    config = node_configuration("cp-01", normalized, normalized["nodes"]["cp-01"])
    endpoint = kubeadm_documents(config)[1]["controlPlaneEndpoint"]
    assert endpoint.endswith(":6443")
    assert "/" not in endpoint
