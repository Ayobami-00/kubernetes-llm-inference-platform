"""Reconcile the three Terraform-owned CPU workers through B's trusted host interface."""

from __future__ import annotations

import base64
import json
import re
import shlex
import uuid
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address

import yaml
from cluster_models import PeerRegistry, WorkerRequest, assert_node_identity
from common import (
    CONFIG,
    PROFILES,
    ROOT,
    Aws,
    ensure_ssh_key,
    state_home,
    tf,
    utc_now,
    write_private,
)
from configuration import validate_platform_configuration
from smoke import ssm_command, wait_until


def validate_allocation(document: dict) -> dict:
    """Allocation is an input, never evidence of live host/Kubernetes readiness."""
    platform = json.loads((ROOT / "infra/terraform/platform.json").read_text())
    validate_platform_configuration(CONFIG, platform)
    identity = {
        "schema_version": "klip-platform-allocation.v1",
        **{key: CONFIG[key] for key in ("cluster_id", "account_id", "region", "project")},
        "instance_type": platform["instance_type"],
    }
    if set(document) != {*identity, "network", "nodes"} or any(
        document.get(key) != value for key, value in identity.items()
    ):
        raise ValueError("Unexpected platform allocation identity")
    if set(document["network"]) != {"vpc_id", "security_group_id"}:
        raise ValueError("Unexpected platform network fields")
    if set(document["nodes"]) != set(platform["nodes"]):
        raise ValueError("Allocation must contain all three reviewed platform slots")
    fields = {
        "instance_id",
        "availability_zone",
        "private_ip",
        "public_ip",
        "elastic_ip_id",
        "eni_id",
        "root_volume_id",
        "wireguard_ip",
        "labels",
    }
    for name, node in document["nodes"].items():
        expected = platform["nodes"][name]
        if set(node) != fields or any(
            node[key] != expected[key]
            for key in ("availability_zone", "private_ip", "wireguard_ip")
        ):
            raise ValueError("Platform allocation differs from its reviewed slot")
        if node["labels"] != {
            "klip.dev/node-role": "platform",
            "topology.kubernetes.io/zone": expected["availability_zone"],
        }:
            raise ValueError("Unexpected platform placement labels")
        if not ip_address(node["public_ip"]).is_global:
            raise ValueError("Platform mesh endpoint must be public")
        for field, prefix in (
            ("instance_id", "i"),
            ("eni_id", "eni"),
            ("elastic_ip_id", "eipalloc"),
            ("root_volume_id", "vol"),
        ):
            if not re.fullmatch(prefix + r"-[a-f0-9]{17}", node[field]):
                raise ValueError("Invalid platform AWS resource identity")
    for field in fields - {"labels"}:
        if len({node[field] for node in document["nodes"].values()}) != 3:
            raise ValueError(f"Duplicate platform {field}")
    return document


def verify_instances(allocation: dict, instances: list, interfaces: list, volumes: list) -> None:
    """Reject drift and foreign resources before obtaining any administrative transport."""
    indexed = {item["InstanceId"]: item for item in instances}
    enis = {item["NetworkInterfaceId"]: item for item in interfaces}
    disks = {item["VolumeId"]: item for item in volumes}
    platform = json.loads((ROOT / "infra/terraform/platform.json").read_text())
    for name, node in allocation["nodes"].items():
        instance = indexed.get(node["instance_id"], {})
        tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
        metadata = instance.get("MetadataOptions", {})
        eni = enis.get(node["eni_id"], {})
        disk = disks.get(node["root_volume_id"], {})
        checks = (
            instance.get("State", {}).get("Name") == "running",
            instance.get("ImageId") == CONFIG["ami_id"],
            instance.get("InstanceType") == allocation["instance_type"],
            instance.get("PrivateIpAddress") == node["private_ip"],
            instance.get("PublicIpAddress") == node["public_ip"],
            instance.get("Placement", {}).get("AvailabilityZone") == node["availability_zone"],
            instance.get("KeyName") == "klip-platform-worker",
            instance.get("IamInstanceProfile", {}).get("Arn")
            == (
                f"arn:aws:iam::{CONFIG['account_id']}:instance-profile/"
                f"{CONFIG['project']}/klip-platform-worker"
            ),
            tags.get("Project") == CONFIG["project"] and tags.get("Node") == name,
            metadata.get("HttpTokens") == "required",
            metadata.get("HttpPutResponseHopLimit") == 1,
            metadata.get("InstanceMetadataTags") == "disabled",
            eni.get("VpcId") == allocation["network"]["vpc_id"],
            eni.get("PrivateIpAddress") == node["private_ip"],
            eni.get("Attachment", {}).get("InstanceId") == node["instance_id"],
            eni.get("Association", {}).get("AllocationId") == node["elastic_ip_id"],
            eni.get("SourceDestCheck") is False,
            {item["GroupId"] for item in eni.get("Groups", [])}
            == {allocation["network"]["security_group_id"]},
            disk.get("Encrypted") is True and disk.get("VolumeType") == "gp3",
            disk.get("Size") == platform["root_volume_gib"],
            disk.get("AvailabilityZone") == node["availability_zone"],
            any(
                item.get("InstanceId") == node["instance_id"] and item.get("State") == "attached"
                for item in disk.get("Attachments", [])
            ),
        )
        if not all(checks):
            raise ValueError(f"Live AWS identity/security/storage mismatch for {name}")


def trusted_access(aws: Aws, node: dict, folder) -> dict:
    """SSM authenticates the first public host key; no ssh-keyscan/TOFU fallback."""
    wait_until(
        lambda: any(
            item.get("PingStatus") == "Online"
            for item in aws.call(
                "ssm",
                "describe-instance-information",
                "--filters",
                json.dumps([{"Key": "InstanceIds", "Values": [node["instance_id"]]}]),
            )["InstanceInformationList"]
        ),
        "platform worker SSM registration",
        timeout=300,
    )
    public = (
        ssm_command(aws, node["instance_id"], "cat /etc/ssh/ssh_host_ed25519_key.pub")["stdout"]
        .strip()
        .split()
    )
    if len(public) not in (2, 3) or public[0] != "ssh-ed25519":
        raise ValueError("SSM did not return exactly one Ed25519 SSH host key")
    blob = base64.b64decode(public[1], validate=True)
    if len(blob) != 51 or blob[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20":
        raise ValueError("Malformed Ed25519 SSH host key")
    known = folder / f"platform-known-hosts-{node['instance_id']}"
    line = f"{node['instance_id']} {public[0]} {public[1]}\n"
    if known.exists() and known.read_text() != line:
        raise ValueError("Previously trusted platform host key changed; explicit recovery required")
    write_private(known, line)
    _, key = ensure_ssh_key()
    proxy = shlex.join(
        [
            aws.executable,
            "ssm",
            "start-session",
            "--target",
            "%h",
            "--document-name",
            "AWS-StartSSHSession",
            "--parameters",
            "portNumber=%p",
            "--profile",
            aws.profile,
            "--region",
            CONFIG["region"],
        ]
    )
    return {
        "ansible_host": node["instance_id"],
        "ansible_user": "ubuntu",
        "ansible_ssh_private_key_file": str(key),
        "ansible_ssh_common_args": "-o StrictHostKeyChecking=yes "
        f"-o UserKnownHostsFile={shlex.quote(str(known))} -o ProxyCommand={shlex.quote(proxy)}",
    }


def publish_peer(folder, name: str, node: dict, public_key: str) -> int:
    registry = PeerRegistry.model_validate_json((folder / "peers.json").read_text())
    peer = {key: node[key] for key in ("instance_id", "public_ip", "private_ip", "wireguard_ip")}
    peer.update(role="worker", public_key=public_key)
    previous = registry.peers.get(name)
    if previous and previous.instance_id != node["instance_id"]:
        raise ValueError("Remove the previous platform mesh identity before replacing a host")
    if previous is None or previous.model_dump(mode="json") != peer:
        document = registry.model_dump(mode="json")
        document["revision"] += 1
        document["peers"][name] = peer
        registry = PeerRegistry.model_validate(document)
        write_private(folder / "peers.json", registry.model_dump(mode="json"))
    return registry.revision


def placement_patch(request: WorkerRequest, node: dict, labels: dict) -> list:
    assert_node_identity(request, node)
    if node.get("status", {}).get("conditions") is None or not any(
        item["type"] == "Ready" and item["status"] == "True"
        for item in node["status"]["conditions"]
    ):
        raise ValueError("Platform node is not Ready")
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": request.node_uid},
        {"op": "test", "path": "/spec/providerID", "value": node["spec"]["providerID"]},
    ]
    for key, value in labels.items():
        patch.append(
            {"op": "add", "path": "/metadata/labels/" + key.replace("/", "~1"), "value": value}
        )
    taints = node["spec"].get("taints", [])
    if any(item["key"] == "klip.dev/bootstrap" for item in taints):
        patch += [
            {"op": "test", "path": "/spec/taints", "value": taints},
            {
                "op": "replace",
                "path": "/spec/taints",
                "value": [item for item in taints if item["key"] != "klip.dev/bootstrap"],
            },
        ]
    return patch


def join_platform() -> dict:
    """Called under cluster.py's operator lock; each explicit retry rechecks live identity."""
    import cluster
    from cluster_workers import operate_worker

    aws = Aws(PROFILES["foundation"])
    aws.preflight("foundation")
    allocation = validate_allocation(
        json.loads(tf("platform", "output", "-json", "platform_manifest"))
    )
    ids = [node["instance_id"] for node in allocation["nodes"].values()]
    instances = [
        item
        for group in aws.call("ec2", "describe-instances", "--instance-ids", *ids)["Reservations"]
        for item in group["Instances"]
    ]
    interfaces = aws.call(
        "ec2",
        "describe-network-interfaces",
        "--network-interface-ids",
        *[node["eni_id"] for node in allocation["nodes"].values()],
    )["NetworkInterfaces"]
    volumes = aws.call(
        "ec2",
        "describe-volumes",
        "--volume-ids",
        *[node["root_volume_id"] for node in allocation["nodes"].values()],
    )["Volumes"]
    verify_instances(allocation, instances, interfaces, volumes)
    foundation = json.loads((state_home() / "reports/foundation.json").read_text())["allocation"]
    if allocation["network"]["vpc_id"] != foundation["network"]["vpc_id"]:
        raise ValueError("Platform and cluster foundation VPCs differ")
    folder = cluster.FOLDER
    cp = cluster.Host(cluster.load_hosts()["cp-01"])
    run_id = uuid.uuid4().hex[:12]
    observations = {}
    for name, node in sorted(allocation["nodes"].items()):
        access = trusted_access(aws, node, folder)
        aliases_path = folder / "credential-references.json"
        aliases = json.loads(aliases_path.read_text()) if aliases_path.exists() else {}
        access_ref = "credential://klip/" + name + "-ssh"
        aliases[access_ref] = access
        write_private(aliases_path, aliases)
        request = WorkerRequest(
            operation_id=f"{name}-{run_id}-prepare",
            deadline=datetime.now(UTC) + timedelta(minutes=20),
            provider="aws",
            provider_instance_id=node["instance_id"],
            node_name=name,
            availability_zone=node["availability_zone"],
            quarantine=True,
            **{key: node[key] for key in ("public_ip", "private_ip", "wireguard_ip")},
            access_ref=access_ref,
            registry_revision=json.loads((folder / "peers.json").read_text())["revision"],
        )
        path = folder / "operations" / f"{request.operation_id}.request.json"
        write_private(path, request.model_dump(mode="json"))
        prior_deadline = cluster.OPERATION_DEADLINE
        try:
            prepared = operate_worker("worker-prepare", path)
            revision = publish_peer(folder, name, node, prepared["public_key"])
            token = cp.run(
                ["sudo", "kubeadm", "token", "create", "--ttl", "15m"], secret=True
            ).strip()
            credential = folder / "operations" / f"{name}-{run_id}.join.json"
            try:
                ca_hash = cp.run(
                    [
                        "sudo",
                        "sh",
                        "-c",
                        "openssl x509 -pubkey -in /etc/kubernetes/pki/ca.crt "
                        "| openssl pkey -pubin -outform der | sha256sum",
                    ]
                ).split()[0]
                if not re.fullmatch(r"[a-f0-9]{64}", ca_hash):
                    raise ValueError("Invalid live cluster CA hash")
                write_private(
                    credential,
                    {
                        "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                        "discovery": {
                            "apiServerEndpoint": foundation["api"]["endpoint"]
                            .removeprefix("https://")
                            .rstrip("/"),
                            "token": token,
                            "caCertHashes": ["sha256:" + ca_hash],
                        },
                    },
                )
                joins = folder / "join-references.json"
                references = json.loads(joins.read_text()) if joins.exists() else {}
                join_ref = "credential://klip/" + name + "-join"
                references[join_ref] = str(credential)
                write_private(joins, references)
                joining = request.model_copy(
                    update={
                        "operation_id": f"{name}-{run_id}-join",
                        "registry_revision": revision,
                        "join_ref": join_ref,
                    }
                )
                path = folder / "operations" / f"{joining.operation_id}.request.json"
                write_private(path, joining.model_dump(mode="json"))
                joined = operate_worker("worker-join", path)
            finally:
                credential.unlink(missing_ok=True)
                # Cleanup must still run when the host operation exhausted its deadline.
                cluster.OPERATION_DEADLINE = prior_deadline
                cp.run(["sudo", "kubeadm", "token", "delete", token.split(".")[0]], secret=True)
            observed = json.loads(cluster.kubectl("get", "node", name, "-o", "json"))
            identity = joining.model_copy(update={"node_uid": joined["node_uid"]})
            host = cluster.Host(cluster.load_hosts()[name])
            kubelet = yaml.safe_load(host.run(["sudo", "cat", "/var/lib/kubelet/config.yaml"]))
            if kubelet.get("systemReserved") != {"cpu": "200m", "memory": "512Mi"} or kubelet.get(
                "kubeReserved"
            ) != {"cpu": "300m", "memory": "768Mi"}:
                raise ValueError("Worker kubelet reservations differ from the platform policy")
            if not all(host.agent("route")["healthy"].values()):
                raise ValueError("Worker cannot reach every configured API gateway")
            patch = placement_patch(identity, observed, node["labels"])
            cluster.kubectl("patch", "node", name, "--type=json", "-p", json.dumps(patch))
            observations[name] = {
                "instance_id": node["instance_id"],
                "node_uid": joined["node_uid"],
                "availability_zone": node["availability_zone"],
                "labels": node["labels"],
                "readiness": joined["readiness"],
                "reservations_verified": True,
                "routes_verified": True,
                "observed_at": utc_now(),
            }
            write_private(
                folder / "platform-workers.json",
                {
                    "schema_version": "klip-platform-observation.v1",
                    "nodes": observations,
                },
            )
        finally:
            cluster.OPERATION_DEADLINE = prior_deadline
    return {"schema_version": "klip-platform-observation.v1", "nodes": observations}
