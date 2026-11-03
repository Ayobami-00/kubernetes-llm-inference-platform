"""Customer requirements; generation controls are distinct from server configuration."""

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .common import (
    ContractModel,
    Count,
    Fraction,
    LatencyMetric,
    NonEmpty,
    PositiveInt,
    PositiveNumber,
    RevisionRef,
    Slug,
    VersionedSpec,
)


class PromptTemplate(ContractModel):
    system: NonEmpty
    user: NonEmpty


class Workload(ContractModel):
    task: Literal["sentence_correction", "coding", "tutoring", "extraction", "research"]
    dataset_ref: RevisionRef
    interaction: Literal["independent_requests", "session"]
    prompt_template: PromptTemplate | None = None
    preserve_recorded_context: bool = False

    @property
    def prompt_template_digest(self) -> str:
        description = {
            "template": self.prompt_template.model_dump(mode="json")
            if self.prompt_template
            else None,
            "preserve_recorded_context": self.preserve_recorded_context,
        }
        canonical = json.dumps(description, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @model_validator(mode="after")
    def check_replay(self) -> Self:
        if self.preserve_recorded_context and self.prompt_template is not None:
            raise ValueError("recorded-context replay cannot override the prompt template")
        if self.preserve_recorded_context and self.interaction != "session":
            raise ValueError("recorded-context replay requires a session workload")
        return self


class Generation(ContractModel):
    max_output_tokens: PositiveInt
    temperature: Annotated[float, Field(strict=True, ge=0, le=2)] = 0.0
    streaming: bool = False


class CustomerLimits(ContractModel):
    max_rendered_input_tokens: PositiveInt
    max_inflight_requests: PositiveInt
    request_timeout_ms: PositiveInt


class TrafficAllowance(ContractModel):
    unit: Literal["request", "session"]
    sustained_per_second: PositiveNumber
    burst_per_second: PositiveNumber
    max_burst_duration_s: PositiveInt = 60

    @model_validator(mode="after")
    def check_burst(self) -> Self:
        if self.burst_per_second < self.sustained_per_second:
            raise ValueError("burst allowance cannot be below sustained allowance")
        return self


class LatencyTarget(ContractModel):
    metric: LatencyMetric
    threshold_ms: PositiveNumber
    minimum_attainment_fraction: Annotated[Fraction, Field(gt=0)] = 0.95


class CustomerSLO(ContractModel):
    window_s: PositiveInt = 300
    minimum_eligible_requests: PositiveInt = 20
    targets: Annotated[tuple[LatencyTarget, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_metrics(self) -> Self:
        if len({target.metric for target in self.targets}) != len(self.targets):
            raise ValueError("SLO metrics must be unique")
        return self


class QualityConfig(ContractModel):
    evaluator: Literal[
        "jfleg_gleu",
        "tutoring_reference_review",
        "extraction_f1",
        "hotpotqa_em_f1",
        "not_evaluated",
    ]
    notes: NonEmpty


class CustomerSpec(VersionedSpec):
    name: NonEmpty
    model_alias: Slug
    tier: Literal["dedicated", "provisioned", "best_effort"]
    reserved_concurrency: Count = 0
    workload: Workload
    generation: Generation
    limits: CustomerLimits
    traffic_allowance: TrafficAllowance
    slo: CustomerSLO
    quality: QualityConfig

    @model_validator(mode="after")
    def compatible_requirements(self) -> Self:
        if self.tier == "provisioned" and self.reserved_concurrency == 0:
            raise ValueError("provisioned tier requires reserved concurrency")
        if self.tier != "provisioned" and self.reserved_concurrency != 0:
            raise ValueError("reserved concurrency applies only to the provisioned tier")
        if self.reserved_concurrency > self.limits.max_inflight_requests:
            raise ValueError("reservation exceeds customer concurrency limit")
        expected_unit = "session" if self.workload.interaction == "session" else "request"
        if self.traffic_allowance.unit != expected_unit:
            raise ValueError("traffic allowance unit must match workload interaction")
        for target in self.slo.targets:
            if target.metric in {"ttft_ms", "tpot_ms"} and not self.generation.streaming:
                raise ValueError("TTFT/TPOT targets require streaming")
            if target.metric == "session_latency_ms" and self.workload.interaction != "session":
                raise ValueError("session latency target requires a session workload")
            if target.metric == "completion_latency_ms":
                if target.threshold_ms > self.limits.request_timeout_ms:
                    raise ValueError("completion deadline exceeds the request timeout")
        return self
