"""The first experiment exposes a fixed reviewed preset, not arbitrary deployment input."""

from datetime import datetime
from typing import Any, Literal

from platform_contracts.common import ContractModel, Sha256
from pydantic import Field


class ExperimentLaunch(ContractModel):
    definition_sha256: Sha256


class ExperimentPreview(ContractModel):
    available: bool
    blockers: list[str] = Field(default_factory=list)
    definition_sha256: Sha256 | None = None
    summary: dict[str, Any] = Field(default_factory=dict)


class ExperimentView(ContractModel):
    id: str
    mode: Literal["real"] = "real"
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    stage: str
    operation_id: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    definition_sha256: Sha256
    summary: dict[str, Any]
    children: dict[str, str] = Field(default_factory=dict)
    stages: dict[str, dict[str, Any]] = Field(default_factory=dict)
    comparison: dict[str, Any] | None = None
    artifacts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    stop_requested: bool = False
    error: dict[str, str] | None = None
