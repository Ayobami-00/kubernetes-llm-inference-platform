from datetime import datetime
from typing import Annotated, Literal

from platform_contracts import (
    DatasetManifest,
    InfrastructureInventory,
    Run,
    RunSnapshot,
    ServingProfile,
    WorkerGroup,
)
from platform_contracts.common import ContractModel, Slug, Timestamp
from platform_contracts.inventory import NetworkInfo, NodeObservation
from pydantic import Field, HttpUrl, model_validator

from platform_management.api.schemas import ReadinessCheck

Provenance = Literal["synthetic", "measured", "unverified"]


class ImportMetadata(ContractModel):
    source: Annotated[str, Field(min_length=1, max_length=200)]
    provenance: Provenance = "unverified"


class FoundationObservation(ContractModel):
    """C's projection of B's foundation.v1 envelope, not a fabricated full inventory."""

    schema_version: Literal["foundation.v1"]
    captured_at: Timestamp
    allocation: dict
    network: NetworkInfo
    nodes: tuple[NodeObservation, ...]


class InventoryImport(ImportMetadata):
    document: InfrastructureInventory | FoundationObservation


class DatasetImport(ImportMetadata):
    document: DatasetManifest


class PreparedBundle(ContractModel):
    id: Slug
    fixture: bool
    snapshot: RunSnapshot


class PreparedImport(ImportMetadata):
    document: PreparedBundle


class RunImport(ImportMetadata):
    document: Run


class ExternalGroup(ContractModel):
    id: Slug
    group: WorkerGroup


class ExternalDeployment(ContractModel):
    id: Slug
    group_id: Slug
    profile: ServingProfile
    kubernetes_uid: Annotated[str, Field(min_length=1)]
    endpoint: HttpUrl


class ExternalCapacity(ContractModel):
    id: Slug
    inventory_id: Slug
    worker_groups: Annotated[tuple[ExternalGroup, ...], Field(min_length=1)]
    deployments: Annotated[tuple[ExternalDeployment, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def check_handoff(self):
        groups = {item.id: item.group for item in self.worker_groups}
        ids = [item.id for item in (*self.worker_groups, *self.deployments)]
        if len(ids) != len(set(ids)):
            raise ValueError("external resource identities must be unique")
        for group in groups.values():
            if group.spec.desired_instances < 1:
                raise ValueError("external capacity requires at least one desired instance")
            if group.fixture or group.status.active_operation_id:
                raise ValueError("external groups must be measured observations without operations")
            if group.spec.instance_type is None or group.spec.region is None:
                raise ValueError("external groups require verified provider selections")
        used = {identity: 0 for identity in groups}
        for item in self.deployments:
            if item.group_id not in groups or item.profile.draft:
                raise ValueError(
                    "external deployments require an included group and pinned profile"
                )
            group, profile = groups[item.group_id].spec, item.profile
            if (
                profile.worker_class != group.worker_class
                or profile.hardware.min_vram_gib_per_gpu > group.hardware.vram_gib_per_gpu
                or profile.hardware.vendor != group.hardware.vendor
                or (
                    profile.hardware.gpu_model
                    and profile.hardware.gpu_model != group.hardware.gpu_model
                )
                or profile.topology.gpus_per_replica > group.hardware.gpu_count
            ):
                raise ValueError("external serving profile does not fit supplied hardware")
            used[item.group_id] += profile.topology.replicas * profile.topology.gpus_per_replica
        if any(
            used[i] > g.spec.desired_instances * g.spec.hardware.gpu_count
            for i, g in groups.items()
        ):
            raise ValueError("external deployments exceed group GPU capacity")
        return self


class ExternalCapacityImport(ImportMetadata):
    provenance: Literal["measured"] = "measured"
    document: ExternalCapacity


class ImportAccepted(ContractModel):
    import_id: str


class ImportView(ContractModel):
    id: str
    kind: str
    source: str
    provenance: Provenance
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    created_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    result: dict = Field(default_factory=dict)


class InventoryView(ContractModel):
    id: str
    cluster_id: str
    source: str
    complete: bool
    fixture: bool
    captured_at: datetime
    current: bool
    document: dict


class PreparedView(ContractModel):
    id: str
    source: str
    fixture: bool
    digest: str
    snapshot: RunSnapshot


class ArtifactView(ContractModel):
    id: str
    uri: str
    sha256: str | None
    status: Literal["verified", "blocked", "unverified"]
    error: str | None
    checked_at: datetime


class IntegrationCapability(ContractModel):
    category: str
    simulation: str
    real: str
    configured: bool


class IntegrationReport(ContractModel):
    checks: list[ReadinessCheck] = Field(default_factory=list)
    cluster_observation: dict | None = None
    adapters: list[IntegrationCapability]
    artifacts_root: str
    inventory_max_age_seconds: int
    real_lambda_mutations: Literal[False] = False
