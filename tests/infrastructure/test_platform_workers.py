"""Trust, identity, placement and credential cleanup for persistent worker joins."""

import base64
import copy
import json
import subprocess
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import cluster
import cluster_workers
import node_agent
import platform_workers as workers
import pytest
import yaml
from cluster_config import kubeadm_documents
from cluster_models import WorkerRequest
from cluster_verification import foundation_nodes
from common import CONFIG, ROOT, tool, write_private
from inventory import build_observation
from platform_acceptance import metadata_connection_blocked


@pytest.fixture
def platform_allocation():
    config = json.loads((ROOT / "infra/terraform/platform.json").read_text())
    nodes = {}
    for index, (name, node) in enumerate(config["nodes"].items(), 1):
        nodes[name] = {
            **{key: node[key] for key in ("availability_zone", "private_ip", "wireguard_ip")},
            **{
                field: f"{prefix}-{index:017x}"
                for field, prefix in (
                    ("instance_id", "i"),
                    ("eni_id", "eni"),
                    ("elastic_ip_id", "eipalloc"),
                    ("root_volume_id", "vol"),
                )
            },
            "public_ip": f"1.1.1.{index}",
            "labels": {
                "klip.dev/node-role": "platform",
                "topology.kubernetes.io/zone": node["availability_zone"],
            },
        }
    return {
        "schema_version": "klip-platform-allocation.v1",
        **{key: CONFIG[key] for key in ("cluster_id", "account_id", "region", "project")},
        "instance_type": config["instance_type"],
        "network": {"vpc_id": "vpc-1", "security_group_id": "sg-1"},
        "nodes": nodes,
    }


@pytest.fixture
def live_resources(platform_allocation):
    instances, enis, volumes = [], [], []
    for name, node in platform_allocation["nodes"].items():
        instances.append(
            {
                "InstanceId": node["instance_id"],
                "State": {"Name": "running"},
                "ImageId": CONFIG["ami_id"],
                "InstanceType": "m7i.xlarge",
                "PrivateIpAddress": node["private_ip"],
                "PublicIpAddress": node["public_ip"],
                "Placement": {"AvailabilityZone": node["availability_zone"]},
                "KeyName": "klip-platform-worker",
                "IamInstanceProfile": {
                    "Arn": f"arn:aws:iam::{CONFIG['account_id']}:instance-profile/"
                    f"{CONFIG['project']}/klip-platform-worker",
                },
                "Tags": [
                    {"Key": "Project", "Value": CONFIG["project"]},
                    {"Key": "Node", "Value": name},
                ],
                "MetadataOptions": {
                    "HttpTokens": "required",
                    "HttpPutResponseHopLimit": 1,
                    "InstanceMetadataTags": "disabled",
                },
            }
        )
        enis.append(
            {
                "NetworkInterfaceId": node["eni_id"],
                "VpcId": "vpc-1",
                "PrivateIpAddress": node["private_ip"],
                "SourceDestCheck": False,
                "Groups": [{"GroupId": "sg-1"}],
                "Attachment": {"InstanceId": node["instance_id"]},
                "Association": {"AllocationId": node["elastic_ip_id"]},
            }
        )
        volumes.append(
            {
                "VolumeId": node["root_volume_id"],
                "Encrypted": True,
                "VolumeType": "gp3",
                "Size": 80,
                "AvailabilityZone": node["availability_zone"],
                "Attachments": [{"InstanceId": node["instance_id"], "State": "attached"}],
            }
        )
    return instances, enis, volumes


def request(node, **overrides):
    return WorkerRequest(
        operation_id="platform-join-test",
        deadline=datetime.now(UTC) + timedelta(minutes=10),
        provider="aws",
        provider_instance_id=node["instance_id"],
        node_name="platform-01",
        public_ip=node["public_ip"],
        private_ip=node["private_ip"],
        wireguard_ip=node["wireguard_ip"],
        availability_zone=node["availability_zone"],
        access_ref="credential://klip/platform-01-ssh",
        registry_revision=1,
        **overrides,
    )


def test_reviewed_allocations_require_exact_slots_and_distinct_identities(platform_allocation):
    workers.validate_allocation(platform_allocation)
    for field, value in (
        ("wireguard_ip", "10.200.0.67"),
        ("availability_zone", "eu-west-2c"),
        ("labels", {"klip.dev/node-role": "gpu"}),
        ("public_ip", "127.0.0.1"),
        ("instance_id", platform_allocation["nodes"]["platform-02"]["instance_id"]),
    ):
        invalid = copy.deepcopy(platform_allocation)
        invalid["nodes"]["platform-01"][field] = value
        with pytest.raises(ValueError):
            workers.validate_allocation(invalid)
    with pytest.raises(ValueError, match="identity"):
        workers.validate_allocation({**platform_allocation, "account_id": "other"})


@pytest.mark.parametrize(
    "resource,field,value",
    [
        (0, "ImageId", "ami-unreviewed"),
        (0, "Tags", []),
        (0, "MetadataOptions", {"HttpTokens": "optional"}),
        (0, "Placement", {"AvailabilityZone": "eu-west-2c"}),
        (1, "SourceDestCheck", True),
        (1, "Association", {}),
        (1, "Groups", [{"GroupId": "sg-foreign"}]),
        (2, "Encrypted", False),
        (2, "Attachments", [{"InstanceId": "i-foreign", "State": "attached"}]),
    ],
)
def test_live_drift_blocks_host_operations(
    platform_allocation, live_resources, resource, field, value
):
    workers.verify_instances(platform_allocation, *live_resources)
    live_resources[resource][0][field] = value
    with pytest.raises(ValueError, match="mismatch"):
        workers.verify_instances(platform_allocation, *live_resources)


def test_ssm_is_the_host_key_authority_and_changed_keys_fail_closed(tmp_path, monkeypatch):
    key = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"a" * 32
    response = {"stdout": "ssh-ed25519 " + base64.b64encode(key).decode() + " ubuntu@host"}
    aws = SimpleNamespace(
        executable="aws",
        profile="foundation",
        call=lambda *args: {
            "InstanceInformationList": [{"PingStatus": "Online"}],
        },
    )
    monkeypatch.setattr(workers, "ssm_command", lambda *args: response)
    monkeypatch.setattr(workers, "ensure_ssh_key", lambda: ("public", tmp_path / "private"))
    node = {"instance_id": "i-123"}
    fields = workers.trusted_access(aws, node, tmp_path)
    assert "StrictHostKeyChecking=yes" in fields["ansible_ssh_common_args"]
    assert "AWS-StartSSHSession" in fields["ansible_ssh_common_args"]
    assert workers.trusted_access(aws, node, tmp_path) == fields
    response["stdout"] = "ssh-ed25519 " + base64.b64encode(key[:-32] + b"b" * 32).decode()
    with pytest.raises(ValueError, match="changed"):
        workers.trusted_access(aws, node, tmp_path)
    response["stdout"] = "ssh-ed25519 invalid"
    with pytest.raises(ValueError):
        workers.trusted_access(aws, node, tmp_path)


def test_join_preserves_zone_and_quarantines_before_admitting_workloads(platform_allocation):
    node = platform_allocation["nodes"]["platform-01"]
    candidate = request(node, quarantine=True)
    config = {**node, "name": candidate.node_name, "provider": "aws", "quarantine": True}
    document = kubeadm_documents(config, join={"token": "fixture"}, control_plane=False)[0]
    registration = document["nodeRegistration"]
    assert {"name": "provider-id", "value": f"aws:///eu-west-2a/{node['instance_id']}"} in (
        registration["kubeletExtraArgs"]
    )
    assert registration["taints"] == [
        {"key": "klip.dev/bootstrap", "value": "true", "effect": "NoSchedule"},
    ]
    with pytest.raises(ValueError, match="Lambda"):
        WorkerRequest.model_validate({**candidate.model_dump(), "provider": "lambda"})


def test_placement_is_uid_guarded_and_preserves_other_taints(platform_allocation):
    allocated = platform_allocation["nodes"]["platform-01"]
    candidate = request(allocated, node_uid="uid-1")
    node = {
        "metadata": {"name": "platform-01", "uid": "uid-1", "labels": {}},
        "spec": {
            "providerID": f"aws:///eu-west-2a/{allocated['instance_id']}",
            "taints": [
                {"key": "klip.dev/bootstrap", "value": "true", "effect": "NoSchedule"},
                {"key": "other", "effect": "NoSchedule"},
            ],
        },
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    patch = workers.placement_patch(candidate, node, allocated["labels"])
    assert patch[0] == {"op": "test", "path": "/metadata/uid", "value": "uid-1"}
    assert {
        "op": "add",
        "path": "/metadata/labels/klip.dev.node-restriction.kubernetes.io~1platform",
        "value": "true",
    } in patch
    assert patch[-1]["value"] == [{"key": "other", "effect": "NoSchedule"}]
    node["metadata"]["uid"] = "replacement"
    with pytest.raises(ValueError, match="UID"):
        workers.placement_patch(candidate, node, allocated["labels"])


def test_failed_join_revokes_token_and_restores_deadline(
    tmp_path, monkeypatch, platform_allocation
):
    folder = tmp_path / "cluster"
    folder.mkdir()
    write_private(
        tmp_path / "reports/foundation.json",
        {
            "allocation": {
                "network": {"vpc_id": "vpc-1"},
                "api": {"endpoint": "https://internal:6443"},
            },
        },
    )
    write_private(folder / "peers.json", {"revision": 1})
    monkeypatch.setattr(workers, "state_home", lambda: tmp_path)
    monkeypatch.setattr(cluster, "FOLDER", folder)
    monkeypatch.setattr(cluster, "OPERATION_DEADLINE", None)
    monkeypatch.setattr(workers, "tf", lambda *args: json.dumps(platform_allocation))
    monkeypatch.setattr(workers, "verify_instances", lambda *args: None)
    monkeypatch.setattr(workers, "trusted_access", lambda *args: {})
    monkeypatch.setattr(workers, "publish_peer", lambda *args: 2)
    monkeypatch.setattr(cluster, "load_hosts", lambda: {"cp-01": {}})
    monkeypatch.setattr(
        workers,
        "Aws",
        lambda *args: SimpleNamespace(
            preflight=lambda *args: None,
            call=lambda *args: {
                "Reservations": [],
                "NetworkInterfaces": [],
                "Volumes": [],
            },
        ),
    )
    commands = []

    def run(arguments, **kwargs):
        commands.append(arguments)
        if "delete" in arguments:
            assert cluster.OPERATION_DEADLINE is None
        if "create" in arguments:
            return "abcdef.0123456789abcdef\n"
        return "a" * 64 + "  -\n"

    monkeypatch.setattr(cluster, "Host", lambda *args: SimpleNamespace(run=run))

    def operate(action, path):
        cluster.OPERATION_DEADLINE = 123
        if action == "worker-join":
            raise RuntimeError("injected join failure")
        value = WorkerRequest.model_validate_json(path.read_text())
        assert value.availability_zone == "eu-west-2a" and value.quarantine
        return {"public_key": "fixture"}

    monkeypatch.setattr(cluster_workers, "operate_worker", operate)
    with pytest.raises(RuntimeError, match="injected"):
        workers.join_platform()
    assert ["sudo", "kubeadm", "token", "delete", "abcdef"] in commands
    assert not list((folder / "operations").glob("*.join.json"))
    assert cluster.OPERATION_DEADLINE is None


def test_mesh_reconciliation_is_idempotent_and_refuses_instance_reuse(
    tmp_path, platform_allocation
):
    registry = {
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
    write_private(tmp_path / "peers.json", registry)
    node = platform_allocation["nodes"]["platform-01"]
    public = base64.b64encode(b"w" * 32).decode()
    assert workers.publish_peer(tmp_path, "platform-01", node, public) == 2
    assert workers.publish_peer(tmp_path, "platform-01", node, public) == 2
    with pytest.raises(ValueError, match="previous"):
        workers.publish_peer(tmp_path, "platform-01", {**node, "instance_id": "i-new"}, public)
    with pytest.raises(ValueError, match="Duplicate"):
        workers.publish_peer(tmp_path, "platform-02", node, public)


def test_foundation_checks_accept_known_workers_without_claiming_their_readiness():
    nodes = [{"metadata": {"name": name}, "spec": {}} for name in CONFIG["nodes"]]
    nodes.append({"metadata": {"name": "platform-01"}, "spec": {"providerID": "aws:///az/i-1"}})
    hosts = {"platform-01": {"klip_node": {"role": "worker", "instance_id": "i-1"}}}
    assert set(foundation_nodes(nodes, hosts)) == set(CONFIG["nodes"])
    with pytest.raises(ValueError, match="Unknown"):
        foundation_nodes(nodes, {})
    with pytest.raises(ValueError, match="Missing"):
        foundation_nodes(nodes[1:], hosts)
    nodes[-1]["metadata"]["labels"] = {"node-role.kubernetes.io/control-plane": ""}
    with pytest.raises(ValueError, match="incompatible"):
        foundation_nodes(nodes, hosts)


@pytest.mark.parametrize(
    "provider,address,prefix,accepted",
    [
        ("aws", "10.40.0.20", 24, True),
        ("lambda", "10.40.0.20", 24, False),
        ("aws", "10.41.0.20", 24, True),
        ("aws", "10.244.0.20", 24, False),
        ("aws", "10.96.0.20", 24, False),
        ("aws", "10.200.0.20", 24, False),
        ("aws", "10.40.0.20", 8, False),
    ],
)
def test_native_network_policy_preserves_external_overlap_checks(
    provider, address, prefix, accepted
):
    config = {
        **{
            key: CONFIG[key]
            for key in (
                "pod_cidr",
                "service_cidr",
                "wireguard_cidr",
                "vpc_cidr",
            )
        },
        "provider": provider,
        "role": "worker",
    }
    addresses = [
        {
            "ifname": "ens5",
            "addr_info": [
                {"family": "inet", "local": address, "prefixlen": prefix},
            ],
        }
    ]
    if accepted:
        node_agent.validate_host_networks(config, addresses)
    else:
        with pytest.raises(ValueError):
            node_agent.validate_host_networks(config, addresses)


@pytest.mark.skipif(
    not (ROOT / "artifacts/platform/charts/cilium/Chart.yaml").is_file()
    or not (ROOT / "artifacts/tools/bin/helm").is_file(),
    reason="Requires checksum-verified platform tooling/cache (cluster-tools)",
)
def test_rendered_network_agent_can_bootstrap_without_releasing_workload_quarantine():
    rendered = subprocess.check_output(
        [
            tool("helm"),
            "template",
            "cilium",
            str(ROOT / "artifacts/platform/charts/cilium"),
            "-f",
            str(ROOT / "deploy/values/cilium.yaml"),
        ],
        text=True,
    )
    agent = next(
        item
        for item in yaml.safe_load_all(rendered)
        if item and item["kind"] == "DaemonSet" and item["metadata"]["name"] == "cilium"
    )
    assert agent["spec"]["updateStrategy"]["rollingUpdate"]["maxUnavailable"] == 1
    assert {"key": "klip.dev/bootstrap", "operator": "Exists", "effect": "NoSchedule"} in (
        agent["spec"]["template"]["spec"]["tolerations"]
    )


def test_ansible_timeout_keeps_partial_logs_in_its_operation(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("ansible", 1, output=b"partial output", stderr=b"error")

    monkeypatch.setattr(cluster.subprocess, "run", fail)
    monkeypatch.setattr(cluster, "FOLDER", tmp_path)
    monkeypatch.setattr(cluster, "OPERATION_DEADLINE", None)
    destination = tmp_path / "operations/op-1-prepare.log"
    with pytest.raises(RuntimeError, match="timed out"):
        cluster.ansible("prepare", log_path=destination)
    assert destination.read_text() == "partial outputerror"
    assert destination.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "prepare.log").exists()


def test_static_release_reconciliation_rejects_unreviewed_names():
    with pytest.raises(ValueError, match="reviewed"):
        cluster.sync_release("untrusted")


def test_operator_tunnel_has_a_dedicated_pinned_ssh_transport():
    fields = {
        "ansible_host": "i-123",
        "ansible_user": "ubuntu",
        "ansible_ssh_private_key_file": "/private/key",
        "ansible_ssh_common_args": "-o UserKnownHostsFile=/private/pins",
    }
    arguments = cluster.Host(fields).ssh_arguments(reuse=False)
    assert "ControlMaster=no" in arguments and "ControlPath=none" in arguments
    assert "StrictHostKeyChecking=yes" in arguments
    assert "UserKnownHostsFile=/private/pins" in arguments
    assert arguments[-1] == "ubuntu@i-123"


@pytest.mark.parametrize(
    "message,blocked",
    [
        ("TIMEOUT\ncommand terminated with exit code 1", True),
        ("dial tcp 169.254.169.254:80: connect: connection refused", True),
        ("Error from server (Forbidden): pods/exec not permitted", False),
        ("Error from server (Timeout): request failed", False),
        ("command terminated with exit code 137", False),
    ],
)
def test_metadata_probe_does_not_hide_transport_or_execution_errors(message, blocked):
    assert metadata_connection_blocked(message) is blocked


def test_cilium_uses_the_current_validated_private_api_endpoint(
    tmp_path, monkeypatch, allocation, aws_resources
):
    observation = build_observation(
        allocation,
        aws_resources[0],
        {node["instance_id"]: True for node in allocation["nodes"].values()},
    ).model_dump(mode="json")
    monkeypatch.setattr(cluster, "state_home", lambda: tmp_path)
    write_private(tmp_path / "reports/foundation.json", observation)
    assert "k8sServiceHost=internal-klip.elb.eu-west-2.amazonaws.com" in (
        cluster.release_arguments("cilium")
    )
    assert "k8sServicePort=6443" in cluster.release_arguments("cilium")
    assert cluster.release_arguments("metrics-server") == "--skip-crds"
    observation["allocation"]["api"]["endpoint"] = "https://unrelated.example.com:6443"
    write_private(tmp_path / "reports/foundation.json", observation)
    with pytest.raises(ValueError, match="private foundation"):
        cluster.release_arguments("cilium")


@pytest.mark.parametrize(
    "error,accepted",
    [
        ("Forbidden: node is not allowed to modify labels", True),
        ("Forbidden: pods/exec denied", False),
        ("connection reset", False),
        (None, False),
    ],
)
def test_protected_label_probe_rejects_network_errors_and_permitted_self_labels(error, accepted):
    def run(arguments):
        assert "--kubeconfig=/etc/kubernetes/kubelet.conf" in arguments
        assert "--dry-run=server" in arguments
        if error:
            raise RuntimeError(error)

    host = SimpleNamespace(run=run)
    if accepted:
        workers.verify_restricted_kubelet(host, "platform-01")
    else:
        with pytest.raises((ValueError, RuntimeError)):
            workers.verify_restricted_kubelet(host, "platform-01")
