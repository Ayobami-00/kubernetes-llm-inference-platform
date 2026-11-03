"""Typed allocation/observation exports; shared runtime inventory requires a verified real CA."""

from __future__ import annotations

import json
import socket
import ssl
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlparse

import yaml
from common import CONFIG, PROFILES, Aws, run, state_home, utc_now, write_private
from platform_contracts.common import ContractModel, NonEmpty, Slug, Timestamp
from platform_contracts.inventory import (
    ClusterInfo,
    InfrastructureInventory,
    NetworkInfo,
    NodeObservation,
    Readiness,
)
from pydantic import HttpUrl, IPvAnyAddress, IPvAnyNetwork, model_validator
from smoke import ssm_command


class AllocatedNode(ContractModel):
    instance_id: NonEmpty
    availability_zone: NonEmpty
    private_ip: IPvAnyAddress
    public_ip: IPvAnyAddress
    elastic_ip_id: NonEmpty
    eni_id: NonEmpty
    etcd_volume_id: NonEmpty
    root_volume_id: NonEmpty
    wireguard_ip: IPvAnyAddress
    ssh_user: NonEmpty
    ssh_credential_ref: NonEmpty


class AllocatedNetwork(NetworkInfo):
    vpc_id: NonEmpty
    vpc_cidr: IPvAnyNetwork
    worker_wireguard_range: tuple[IPvAnyAddress, IPvAnyAddress]
    api_routed_cidrs: tuple[IPvAnyNetwork, ...]
    cpu_subnet_ids: dict[Slug, NonEmpty]
    api_subnet_ids: dict[Slug, NonEmpty]
    cpu_security_group_id: NonEmpty
    api_security_group_id: NonEmpty


class AllocatedApi(ContractModel):
    endpoint: HttpUrl
    dns_name: NonEmpty
    arn: NonEmpty
    target_group_arn: NonEmpty
    frontend_ips: tuple[IPvAnyAddress, ...]


class FoundationAllocation(ContractModel):
    schema_version: Literal["foundation-allocation.v1"]
    cluster_id: Slug
    account_id: NonEmpty
    region: NonEmpty
    project: NonEmpty
    ami_id: NonEmpty
    instance_type: NonEmpty
    deletion_protection: bool
    network: AllocatedNetwork
    api: AllocatedApi
    inference_endpoint: HttpUrl
    nodes: dict[Slug, AllocatedNode]

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if (self.account_id, self.region, self.project, self.cluster_id) != (
            CONFIG["account_id"],
            CONFIG["region"],
            CONFIG["project"],
            CONFIG["cluster_id"],
        ):
            raise ValueError("Allocation belongs to a different account, region or cluster")
        if set(self.nodes) != set(CONFIG["nodes"]):
            raise ValueError("Allocation must contain exactly the configured CPU slots")
        for field in (
            "instance_id",
            "eni_id",
            "etcd_volume_id",
            "root_volume_id",
            "public_ip",
            "wireguard_ip",
        ):
            if len({str(getattr(node, field)) for node in self.nodes.values()}) != 3:
                raise ValueError(f"Duplicate allocated {field}")
        volumes = [
            volume
            for node in self.nodes.values()
            for volume in (node.root_volume_id, node.etcd_volume_id)
        ]
        if len(set(volumes)) != 6:
            raise ValueError("Root and etcd volumes must be distinct")
        for name, node in self.nodes.items():
            if (
                str(node.wireguard_ip) != CONFIG["nodes"][name]["wireguard_ip"]
                or node.wireguard_ip not in self.network.wireguard_cidr
            ):
                raise ValueError(
                    "Allocated WireGuard assignment disagrees with the configured slot"
                )
            if node.ssh_credential_ref != CONFIG["ssh_credential_ref"]:
                raise ValueError(
                    "Unexpected SSH credential reference; inline credentials are forbidden"
                )
        return self


class FoundationObservation(ContractModel):
    schema_version: Literal["foundation.v1"] = "foundation.v1"
    captured_at: Timestamp
    allocation: FoundationAllocation
    network: NetworkInfo
    nodes: tuple[NodeObservation, ...]

    @model_validator(mode="after")
    def consistent_observations(self) -> Self:
        if {node.id for node in self.nodes} != set(self.allocation.nodes):
            raise ValueError("Observation does not cover the allocated slots exactly")
        if len(self.nodes) != 3:
            raise ValueError("Duplicate or missing node observations")
        for node in self.nodes:
            allocated = self.allocation.nodes[node.id]
            if (
                node.provider != "aws"
                or node.managed_by != "foundation"
                or node.role != "control_plane"
            ):
                raise ValueError("The foundation manifest contains CPU foundation hosts only")
            if (node.provider_instance_id, node.public_ip, node.wireguard_ip) != (
                allocated.instance_id,
                allocated.public_ip,
                allocated.wireguard_ip,
            ):
                raise ValueError("Observation identity or addresses disagree with allocation")
            if node.observed_at > self.captured_at:
                raise ValueError("Node observation is newer than the capture")
            if node.readiness.kubernetes and (not node.kubernetes or not node.readiness.wireguard):
                raise ValueError(
                    "Kubernetes readiness requires a real node identity and verified WireGuard"
                )
            if node.readiness.cilium and not node.readiness.kubernetes:
                raise ValueError("Cilium readiness requires Kubernetes readiness")
            if node.readiness.gpu_runtime:
                raise ValueError("CPU hosts cannot report GPU runtime readiness")
        return self


def build_observation(
    allocation: dict,
    instances: list[dict],
    host_checks: dict[str, bool],
    *,
    captured_at: str | None = None,
) -> FoundationObservation:
    allocated = FoundationAllocation.model_validate(allocation)
    observed_at = captured_at or utc_now()
    indexed = {item["InstanceId"]: item for item in instances}
    nodes = []
    for name, node in allocated.nodes.items():
        instance = indexed.get(node.instance_id)
        if not instance:
            raise ValueError(f"Allocated instance {name} is missing from AWS observations")
        tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
        if (instance.get("PrivateIpAddress"), instance.get("PublicIpAddress")) != (
            str(node.private_ip),
            str(node.public_ip),
        ):
            raise ValueError("Live addresses differ from the allocated addresses")
        if tags.get("Project") != CONFIG["project"] or tags.get("Node") != name:
            raise ValueError("Observed host lacks expected ownership tags")
        nodes.append(
            NodeObservation.model_validate(
                {
                    "id": name,
                    "provider": "aws",
                    "provider_instance_id": node.instance_id,
                    "managed_by": "foundation",
                    "role": "control_plane",
                    "public_ip": str(node.public_ip),
                    "wireguard_ip": str(node.wireguard_ip),
                    "observed_at": observed_at,
                    "access": {
                        "ssh_user": node.ssh_user,
                        "ssh_credential_ref": node.ssh_credential_ref,
                    },
                    "readiness": Readiness(
                        host=instance["State"]["Name"] == "running"
                        and host_checks.get(node.instance_id, False)
                    ),
                }
            )
        )
    network = NetworkInfo.model_validate(
        allocated.network.model_dump(include={"wireguard_cidr", "pod_cidr", "service_cidr"})
    )
    return FoundationObservation(
        captured_at=observed_at, allocation=allocated, network=network, nodes=tuple(nodes)
    )


def shared_inventory(
    observation: FoundationObservation, ca_ref: str | None, bootstrap_nodes: list[dict] | None
) -> InfrastructureInventory:
    if not ca_ref or bootstrap_nodes is None:
        raise ValueError(
            "Full InfrastructureInventory requires a real verified CA and bootstrap observations"
        )
    nodes = tuple(NodeObservation.model_validate(node) for node in bootstrap_nodes)
    # Reuse foundation identity and readiness consistency checks for the bootstrap observations.
    verified = FoundationObservation(
        captured_at=observation.captured_at,
        allocation=observation.allocation,
        network=observation.network,
        nodes=nodes,
    )
    return InfrastructureInventory(
        snapshot_id=f"klip-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}",
        captured_at=verified.captured_at,
        cluster=ClusterInfo(
            id=verified.allocation.cluster_id,
            api_endpoint=verified.allocation.api.endpoint,
            inference_endpoint=verified.allocation.inference_endpoint,
            ca_bundle_ref=ca_ref,
        ),
        network=verified.network,
        nodes=verified.nodes,
    )


def verify_ca(ca_bundle: Path, api_endpoint: str) -> str:
    if not ca_bundle.is_file() or "PRIVATE KEY" in ca_bundle.read_text():
        raise ValueError("Supply an existing public CA certificate, never a private key")
    output = run(["openssl", "x509", "-in", str(ca_bundle), "-noout", "-text", "-checkend", "0"])
    if "CA:TRUE" not in output:
        raise ValueError("The supplied certificate is not a CA")
    endpoint = urlparse(api_endpoint)
    context = ssl.create_default_context(cafile=str(ca_bundle))
    with socket.create_connection(
        (endpoint.hostname, endpoint.port or 443), timeout=10
    ) as connection:
        with context.wrap_socket(connection, server_hostname=endpoint.hostname):
            pass
    return ca_bundle.resolve().as_uri()


def ansible_inventory(
    observation: FoundationObservation, private_key: Path, known_hosts: Path | None = None
) -> dict:
    import shlex

    hosts = {}
    for node in observation.nodes:
        allocated = observation.allocation.nodes[node.id]
        proxy = [
            "aws",
            "ssm",
            "start-session",
            "--target",
            "%h",
            "--document-name",
            "AWS-StartSSHSession",
            "--parameters",
            "portNumber=%p",
            "--profile",
            PROFILES["foundation"],
            "--region",
            CONFIG["region"],
        ]
        hosts[node.id] = {
            "ansible_host": node.provider_instance_id,
            "ansible_user": node.access.ssh_user,
            "ansible_ssh_private_key_file": str(private_key),
            "ansible_ssh_common_args": (
                "-o StrictHostKeyChecking=yes "
                + (
                    "-o UserKnownHostsFile=" + shlex.quote(str(known_hosts)) + " "
                    if known_hosts
                    else ""
                )
                + "-o ProxyCommand="
                + shlex.quote(shlex.join(proxy))
            ),
            "wireguard_ip": str(node.wireguard_ip),
            "native_private_ip": str(allocated.private_ip),
            "public_ip": str(node.public_ip),
            "etcd_volume_id": allocated.etcd_volume_id,
        }
    return {
        "all": {
            "children": {"control_plane": {"hosts": hosts}},
            "vars": {
                "cluster_id": observation.allocation.cluster_id,
                "api_endpoint": str(observation.allocation.api.endpoint),
                "api_routed_cidrs": [
                    str(cidr) for cidr in observation.allocation.network.api_routed_cidrs
                ],
                "wireguard_cidr": str(observation.network.wireguard_cidr),
                "pod_cidr": str(observation.network.pod_cidr),
                "service_cidr": str(observation.network.service_cidr),
            },
        }
    }


def export_inventory(
    allocation: dict, *, ca_bundle: Path | None = None, bootstrap_observations: Path | None = None
) -> FoundationObservation:
    if (ca_bundle is None) != (bootstrap_observations is None):
        raise ValueError(
            "Provide both --ca-bundle and --bootstrap-observations for full shared export"
        )
    from verification import verify_aws

    aws = Aws(PROFILES["foundation"])
    aws.preflight("foundation")
    allocated = FoundationAllocation.model_validate(allocation)
    instances = verify_aws(aws, allocation)
    checks, failures, evidence, host_keys = {}, [], {}, []
    for node in allocated.nodes.values():
        try:
            result = ssm_command(
                aws,
                node.instance_id,
                "test -s /home/ubuntu/.ssh/authorized_keys\n"
                "cat /etc/ssh/ssh_host_ed25519_key.pub\n",
            )
            fields = result["stdout"].strip().split()
            if len(fields) < 2 or fields[0] != "ssh-ed25519":
                raise ValueError("Missing expected SSH host public key")
            host_keys.append(f"{node.instance_id} {fields[0]} {fields[1]}")
            checks[node.instance_id] = True
            evidence[node.instance_id] = {
                "command_id": result["command_id"],
                "observed_at": result["observed_at"],
            }
        except (RuntimeError, ValueError) as error:
            checks[node.instance_id] = False
            failures.append(str(error))
    observed = build_observation(allocation, instances, checks)
    destination = state_home() / "reports"
    write_private(destination / "foundation.json", observed.model_dump(mode="json"))
    write_private(destination / "host-checks.json", {"checks": evidence, "failures": failures})
    credentials = json.loads((state_home() / "credentials.json").read_text())
    key = Path(credentials[CONFIG["ssh_credential_ref"]]["private_key_path"])
    known_hosts = destination / "known_hosts"
    write_private(known_hosts, "\n".join(host_keys) + "\n")
    write_private(
        destination / "ansible-inventory.yaml",
        yaml.safe_dump(ansible_inventory(observed, key, known_hosts), sort_keys=False),
    )
    if failures:
        raise ValueError(
            "Host checks failed; truthful partial observations saved "
            "in the private reports directory"
        )
    if ca_bundle and bootstrap_observations:
        ca_ref = verify_ca(ca_bundle, str(allocated.api.endpoint))
        shared = shared_inventory(observed, ca_ref, json.loads(bootstrap_observations.read_text()))
        write_private(destination / "infrastructure-inventory.json", shared.model_dump(mode="json"))
    print(f"Validated foundation observations and Ansible inventory: {destination}")
    return observed
