from datetime import datetime
from typing import Annotated, Any, Literal

from platform_contracts import (
    CustomerSpec,
    DatasetManifest,
    OperationStatus,
    Run,
    ScenarioSpec,
    ServingBinding,
    ServingProfile,
    WorkerGroup,
    WorkerGroupSpec,
)
from platform_contracts.common import ContractModel, RevisionRef, Slug
from pydantic import Field

SpecDocument = CustomerSpec | ScenarioSpec | ServingProfile | WorkerGroupSpec | DatasetManifest
Fault = Literal["none", "shortage", "transient", "partial", "ambiguous", "termination"]
Outcome = Literal["passing", "violating", "insufficient_data", "invalid", "failed"]


class Login(ContractModel):
    username: str
    password: Annotated[str, Field(min_length=1, max_length=1024)]


class SessionInfo(ContractModel):
    username: Literal["admin"] = "admin"
    csrf_token: str
    expires_at: datetime


class SpecWrite(ContractModel):
    document: SpecDocument
    expected_version: int = 0


class SpecView(ContractModel):
    id: str
    kind: str
    current_revision: int
    version: int
    document: SpecDocument | None
    draft: SpecDocument | None


class Publish(ContractModel):
    expected_version: int


class Accepted(ContractModel):
    operation_id: str
    resource_id: str


class CreateGroup(ContractModel):
    id: Slug
    spec_ref: RevisionRef
    fault: Fault = "none"
    mode: Literal["simulation"] = "simulation"


class GroupView(ContractModel):
    id: str
    mode: Literal["simulation"] = "simulation"
    group: WorkerGroup
    fault: Fault = "none"


class RenewGroup(ContractModel):
    additional_minutes: Annotated[int, Field(strict=True, ge=1, le=1440)] = 30


class DeploymentWrite(ContractModel):
    id: Slug
    profile_ref: RevisionRef
    group_id: Slug
    mode: Literal["simulation"] = "simulation"


class DeploymentView(ContractModel):
    id: str
    profile: ServingProfile
    group_id: str
    status: Literal["deploying", "ready", "failed", "unavailable"]
    mode: Literal["simulation"] = "simulation"


class RunLaunch(ContractModel):
    scenario_ref: RevisionRef
    prepared_run_id: Slug | None = None
    mode: Literal["simulation", "real"] = "simulation"
    outcome: Outcome = "passing"
    group_ids: dict[str, str] = Field(default_factory=dict)
    bindings: list[ServingBinding] | None = None


class RunView(ContractModel):
    run: Run
    mode: Literal["simulation", "real"]
    group_ids: dict[str, str] = Field(default_factory=dict)
    source: str = "management"
    provenance: Literal["synthetic", "measured", "unverified"] = "synthetic"


class ReadinessCheck(ContractModel):
    code: str
    label: str
    status: Literal["ready", "blocked", "unconfigured", "stale"]
    resource_ref: str | None = None
    detail: str


class Preflight(ContractModel):
    executable: bool
    contract_blockers: list[str]
    runtime_blockers: list[str]
    simulation_available: bool = True
    checks: list[ReadinessCheck] = Field(default_factory=list)


class EventView(ContractModel):
    created_at: datetime
    message: str


class OperationView(ContractModel):
    operation: OperationStatus
    events: list[EventView]
    attempt: int


class Capacity(ContractModel):
    source: Literal["lambda"] = "lambda"
    status: Literal["unconfigured", "fresh", "stale", "error"] = "unconfigured"
    observed_at: datetime | None = None
    attempted_at: datetime | None = None
    error: str | None = None
    instance_types: list[dict[str, Any]] = Field(default_factory=list)
    instances: list[dict[str, Any]] = Field(default_factory=list)


class GraphNode(ContractModel):
    id: str
    kind: str
    label: str
    subtitle: str
    status: str
    provenance: Literal["configuration", "simulation", "live", "historical", "imported"]
    resource_id: str
    document: dict[str, Any]


class GraphEdge(ContractModel):
    id: str
    source: str
    target: str
    label: str


class Topology(ContractModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    historical: bool = False
    captured_at: datetime
    counts: dict[str, int]


class ErrorDetail(ContractModel):
    path: list[str | int]
    message: str


class ErrorBody(ContractModel):
    code: str
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)


class ErrorEnvelope(ContractModel):
    error: ErrorBody
