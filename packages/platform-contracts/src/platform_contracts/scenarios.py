"""Traffic is admission traffic: session turns remain response dependent."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .common import (
    ContractModel,
    Count,
    PositiveInt,
    PositiveNumber,
    RevisionRef,
    Slug,
    VersionedSpec,
)


class Arrival(ContractModel):
    pattern: Literal["poisson", "constant"] = "poisson"
    unit: Literal["request", "session"]
    rate: PositiveNumber
    per: Literal["second", "minute"] = "second"

    @property
    def per_second(self) -> float:
        return self.rate / (60 if self.per == "minute" else 1)


class Warmup(ContractModel):
    duration_s: Count = 60
    datasets: dict[Slug, RevisionRef] = Field(default_factory=dict)


class Phase(ContractModel):
    id: Slug
    duration_s: PositiveInt
    arrivals: dict[Slug, Arrival]


class Fault(ContractModel):
    kind: Literal["pod_delete"]
    customer_id: Slug
    offset_s: Count


class ScenarioSpec(VersionedSpec):
    seed: Count = 42
    participants: Annotated[tuple[RevisionRef, ...], Field(min_length=1)]
    warmup: Warmup = Field(default_factory=Warmup)
    phases: Annotated[tuple[Phase, ...], Field(min_length=1)]
    faults: tuple[Fault, ...] = ()
    drain_timeout_s: PositiveInt = 120

    @property
    def measurement_duration_s(self) -> int:
        return sum(phase.duration_s for phase in self.phases)

    @model_validator(mode="after")
    def check_participants(self) -> Self:
        ids = [ref.rsplit("@", 1)[0] for ref in self.participants]
        if len(set(ids)) != len(ids):
            raise ValueError("scenario contains duplicate customer identities")
        if len({phase.id for phase in self.phases}) != len(self.phases):
            raise ValueError("phase IDs must be unique")
        for phase in self.phases:
            if set(phase.arrivals) != set(ids):
                raise ValueError("every phase must specify arrivals for exactly the participants")
        if not set(self.warmup.datasets).issubset(ids):
            raise ValueError("warmup datasets contain an unknown customer")
        for fault in self.faults:
            if fault.customer_id not in ids or fault.offset_s >= self.measurement_duration_s:
                raise ValueError("fault target or offset is outside the scenario")
        return self
