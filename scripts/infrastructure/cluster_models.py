"""Stream B operation/mesh inputs, separate from the shared runtime contracts."""

from __future__ import annotations

import base64
import ipaddress
from datetime import UTC, datetime
from typing import Literal, Self

from platform_contracts.common import ContractModel, NonEmpty, Slug, Timestamp
from pydantic import Field, IPvAnyAddress, IPvAnyNetwork, model_validator


class Peer(ContractModel):
    instance_id: NonEmpty
    role: Literal["control_plane", "worker", "probe"]
    public_ip: IPvAnyAddress
    private_ip: IPvAnyAddress
    wireguard_ip: IPvAnyAddress
    public_key: NonEmpty

    @model_validator(mode="after")
    def valid_peer(self) -> Self:
        if len(base64.b64decode(self.public_key, validate=True)) != 32:
            raise ValueError("WireGuard public key must contain 32 bytes")
        if self.public_ip.version != 4 or not self.public_ip.is_global:
            raise ValueError("External mesh endpoint must be a public IPv4 address")
        address = int(self.wireguard_ip)
        network = ipaddress.ip_network("10.200.0.0/24")
        if self.wireguard_ip not in network or address in (
            int(network.network_address),
            int(network.broadcast_address),
        ):
            raise ValueError("Invalid WireGuard host allocation")
        if self.role == "worker" and not 64 <= int(str(self.wireguard_ip).split(".")[-1]) <= 254:
            raise ValueError("Workers must use the reserved .64-.254 range")
        return self


class PeerRegistry(ContractModel):
    schema_version: Literal["klip-peers.v1"] = "klip-peers.v1"
    cluster_id: Literal["klip"] = "klip"
    revision: int = Field(ge=1, strict=True)
    peers: dict[Slug, Peer]

    @model_validator(mode="after")
    def unique_peers(self) -> Self:
        for field in ("instance_id", "public_key", "wireguard_ip"):
            if len({str(getattr(peer, field)) for peer in self.peers.values()}) != len(self.peers):
                raise ValueError(f"Duplicate mesh {field}")
        for name, expected in (
            ("cp-01", "10.200.0.11"),
            ("cp-02", "10.200.0.12"),
            ("cp-03", "10.200.0.13"),
        ):
            if (
                name not in self.peers
                or self.peers[name].role != "control_plane"
                or str(self.peers[name].wireguard_ip) != expected
            ):
                raise ValueError("CPU peer assignments are immutable")
        return self


class WorkerRequest(ContractModel):
    schema_version: Literal["klip-worker-operation.v1"] = "klip-worker-operation.v1"
    cluster_id: Literal["klip"] = "klip"
    operation_id: Slug
    deadline: Timestamp
    provider: Literal["lambda", "aws"]
    availability_zone: str | None = Field(default=None, pattern=r"^[a-z]{2}-[a-z]+-[0-9][a-z]$")
    quarantine: bool = False
    provider_instance_id: NonEmpty
    node_name: Slug
    node_uid: NonEmpty | None = None
    public_ip: IPvAnyAddress
    private_ip: IPvAnyAddress
    wireguard_ip: IPvAnyAddress
    access_ref: NonEmpty
    join_ref: NonEmpty | None = None
    registry_revision: int = Field(ge=1, strict=True)
    drain_timeout_s: int = Field(default=120, ge=1, le=600)
    ssh_source_cidrs: tuple[IPvAnyNetwork, ...] = ()

    @model_validator(mode="after")
    def restricted_input(self) -> Self:
        if self.provider != "aws" and self.availability_zone is not None:
            raise ValueError("AWS availability-zone metadata cannot describe a Lambda worker")
        if self.node_name.startswith("cp-"):
            raise ValueError("Worker operations cannot target control-plane nodes")
        for ref in (self.access_ref, self.join_ref):
            if ref is not None and not ref.startswith("credential://klip/"):
                raise ValueError("Credentials must be references resolved outside job payloads")
        if any(
            network.version != 4 or network.prefixlen != 32 or not network.network_address.is_global
            for network in self.ssh_source_cidrs
        ):
            raise ValueError("Lambda SSH administration requires explicit public /32 sources")
        octet = int(str(self.wireguard_ip).split(".")[-1])
        if self.wireguard_ip not in ipaddress.ip_network("10.200.0.0/24") or not 64 <= octet <= 254:
            raise ValueError("Worker address is outside the reserved allocation range")
        return self

    def remaining(self) -> int:
        seconds = int((self.deadline - datetime.now(UTC)).total_seconds())
        if seconds <= 0:
            raise ValueError("Operation deadline has expired")
        return seconds


def assert_node_identity(request: WorkerRequest, node: dict) -> None:
    metadata = node["metadata"]
    if metadata["name"] != request.node_name or (
        request.node_uid and metadata["uid"] != request.node_uid
    ):
        raise ValueError("Kubernetes node UID/name mismatch; removal refused")
    if node["spec"].get("providerID", "").split("/")[-1] != request.provider_instance_id:
        raise ValueError("Provider instance identity mismatch; operation refused")
    if any(
        key.startswith("node-role.kubernetes.io/control-plane")
        for key in metadata.get("labels", {})
    ):
        raise ValueError("Worker operation cannot modify a control-plane member")
