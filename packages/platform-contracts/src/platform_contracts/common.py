"""Small shared types. All durations have explicit units; timestamps normalize to UTC."""

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
)

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")]
RevisionRef = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*@[1-9][0-9]*$")
]
NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Count = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
PositiveNumber = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
Fraction = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
ArtifactURI = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9+.-]*://\S+$")]


def _timestamp_input(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}[Tt ]", value):
        return value
    raise ValueError("timestamp must be an ISO 8601 datetime with an explicit timezone")


Timestamp = Annotated[
    AwareDatetime,
    BeforeValidator(_timestamp_input),
    AfterValidator(lambda value: value.astimezone(UTC)),
]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True, allow_inf_nan=False)


class VersionedSpec(ContractModel):
    schema_version: Literal["0.1"] = "0.1"
    id: Slug
    revision: PositiveInt = 1
    notes: NonEmpty | None = None

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.revision}"


class RuntimeRecord(ContractModel):
    schema_version: Literal["0.1"] = "0.1"
    fixture: bool = False


class ExecutionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SLOVerdict(StrEnum):
    PASSING = "passing"
    VIOLATING = "violating"
    INSUFFICIENT_DATA = "insufficient_data"
    NOT_EVALUATED = "not_evaluated"


LatencyMetric = Literal["completion_latency_ms", "ttft_ms", "tpot_ms", "session_latency_ms"]


class ErrorInfo(ContractModel):
    code: NonEmpty
    message: NonEmpty


class Artifact(ContractModel):
    uri: ArtifactURI
    sha256: Sha256 | None = None


class GPUHardware(ContractModel):
    vendor: Literal["nvidia"] = "nvidia"
    gpu_model: NonEmpty
    gpu_count: PositiveInt
    vram_gib_per_gpu: PositiveNumber


def is_commit_pin(value: str | None) -> bool:
    return value is not None and re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", value) is not None


def is_image_pin(value: str | None) -> bool:
    return value is not None and re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", value) is not None


def validate_lifecycle(
    status: ExecutionStatus,
    created_at: datetime,
    started_at: datetime | None,
    finished_at: datetime | None,
) -> None:
    if started_at is not None and started_at < created_at:
        raise ValueError("started_at precedes created_at")
    if finished_at is not None and finished_at < (started_at or created_at):
        raise ValueError("finished_at precedes execution")
    if status == ExecutionStatus.QUEUED and (started_at is not None or finished_at is not None):
        raise ValueError("queued execution cannot have started or finished")
    if status == ExecutionStatus.RUNNING and (started_at is None or finished_at is not None):
        raise ValueError("running execution requires started_at and no finished_at")
    if status in {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}:
        if finished_at is None:
            raise ValueError("terminal execution requires finished_at")
    if status == ExecutionStatus.COMPLETED and started_at is None:
        raise ValueError("completed execution requires started_at")
