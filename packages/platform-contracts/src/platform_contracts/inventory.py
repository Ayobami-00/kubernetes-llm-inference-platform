"""Inventory is an observation. It never carries inline bootstrap secrets."""

from typing import Literal, Self

from pydantic import HttpUrl, IPvAnyAddress, IPvAnyNetwork, model_validator

from .common import (
    ArtifactURI,
    ContractModel,
    Count,
    GPUHardware,
    NonEmpty,
    RevisionRef,
    RuntimeRecord,
    Slug,
    Timestamp,
)


class ClusterInfo(ContractModel):
    id: Slug
    api_endpoint: HttpUrl
    inference_endpoint: HttpUrl
    ca_bundle_ref: ArtifactURI


class NetworkInfo(ContractModel):
    wireguard_cidr: IPvAnyNetwork
    pod_cidr: IPvAnyNetwork
    service_cidr: IPvAnyNetwork

    @model_validator(mode="after")
    def non_overlapping_cidrs(self) -> Self:
        networks = [self.wireguard_cidr, self.pod_cidr, self.service_cidr]
        for index, network in enumerate(networks):
            for other in networks[index + 1 :]:
                if network.version == other.version and network.overlaps(other):
                    raise ValueError("WireGuard, Pod and Service CIDRs must not overlap")
        return self


class KubernetesNode(ContractModel):
    name: NonEmpty
    uid: NonEmpty
    allocatable_gpu_count: Count = 0
    allocated_gpu_count: Count = 0

    @model_validator(mode="after")
    def check_allocation(self) -> Self:
        if self.allocated_gpu_count > self.allocatable_gpu_count:
            raise ValueError("allocated GPUs exceed allocatable GPUs")
        return self


class HostAccess(ContractModel):
    ssh_user: NonEmpty
    ssh_credential_ref: NonEmpty


class Readiness(ContractModel):
    host: bool = False
    wireguard: bool = False
    kubernetes: bool = False
    cilium: bool = False
    gpu_runtime: bool = False


class NodeObservation(ContractModel):
    id: Slug
    provider: Literal["aws", "lambda"]
    provider_instance_id: NonEmpty
    managed_by: Literal["foundation", "management", "external"]
    role: Literal["control_plane", "gpu_worker"]
    public_ip: IPvAnyAddress
    wireguard_ip: IPvAnyAddress
    observed_at: Timestamp
    access: HostAccess
    readiness: Readiness
    kubernetes: KubernetesNode | None = None
    hardware: GPUHardware | None = None
    worker_group_ref: RevisionRef | None = None

    @model_validator(mode="after")
    def check_role(self) -> Self:
        if self.role == "control_plane":
            if self.provider != "aws" or self.managed_by != "foundation":
                raise ValueError("control-plane nodes belong to the AWS foundation")
            if self.worker_group_ref is not None or self.hardware is not None:
                raise ValueError("CPU control-plane node cannot carry a GPU worker assignment")
            if self.kubernetes and self.kubernetes.allocatable_gpu_count:
                raise ValueError("CPU node cannot report allocatable GPUs")
        else:
            if self.provider != "lambda" or self.managed_by not in {"management", "external"}:
                raise ValueError("GPU workers belong to Lambda management")
            if self.worker_group_ref is None or self.hardware is None:
                raise ValueError("GPU worker requires hardware and worker-group identity")
        if self.hardware and self.kubernetes:
            if self.kubernetes.allocatable_gpu_count > self.hardware.gpu_count:
                raise ValueError("allocatable GPUs exceed physical GPUs")
        return self


class InfrastructureInventory(RuntimeRecord):
    snapshot_id: Slug
    captured_at: Timestamp
    cluster: ClusterInfo
    network: NetworkInfo
    nodes: tuple[NodeObservation, ...]

    @model_validator(mode="after")
    def check_identities(self) -> Self:
        identities = [
            [node.id for node in self.nodes],
            [(node.provider, node.provider_instance_id) for node in self.nodes],
            [str(node.wireguard_ip) for node in self.nodes],
            [node.kubernetes.uid for node in self.nodes if node.kubernetes],
            [node.kubernetes.name for node in self.nodes if node.kubernetes],
        ]
        if any(len(values) != len(set(values)) for values in identities):
            raise ValueError("inventory contains duplicate node identities or addresses")
        for node in self.nodes:
            if node.wireguard_ip not in self.network.wireguard_cidr:
                raise ValueError("node WireGuard address is outside the host network")
            if node.observed_at > self.captured_at:
                raise ValueError("node observation is newer than inventory capture")
        return self
