from datetime import datetime
from typing import Annotated, Literal

from platform_contracts import DatasetManifest, InfrastructureInventory, Run, RunSnapshot
from platform_contracts.common import ContractModel, Slug, Timestamp
from platform_contracts.inventory import NetworkInfo, NodeObservation
from pydantic import Field

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
