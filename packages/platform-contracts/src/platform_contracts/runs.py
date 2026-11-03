"""Frozen inputs and execution state share one run resource; results remain historical."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import Field, model_validator

from .common import (
    Artifact,
    ContractModel,
    Count,
    ErrorInfo,
    ExecutionStatus,
    Fraction,
    LatencyMetric,
    NonEmpty,
    PositiveInt,
    PositiveNumber,
    RevisionRef,
    RuntimeRecord,
    SLOVerdict,
    Slug,
    Timestamp,
    validate_lifecycle,
)
from .customers import CustomerSpec
from .datasets import DatasetManifest
from .inventory import InfrastructureInventory
from .measurements import (
    DeliveryPolicy,
    ExecutionTarget,
    Milliseconds,
    PreparationManifest,
    SessionCounts,
)
from .scenarios import ScenarioSpec
from .serving import ServingProfile
from .worker_groups import WorkerGroupSpec

if TYPE_CHECKING:
    from .loading import ContractCatalog


class ServingBinding(ContractModel):
    customer_ref: RevisionRef
    serving_profile_ref: RevisionRef
    worker_group_ref: RevisionRef
    deployment_id: Slug


class ExecutionVersions(ContractModel):
    runner_image: NonEmpty | None = None
    aiperf_version: NonEmpty | None = None
    evaluator_version: NonEmpty | None = None
    platform_versions: dict[Slug, NonEmpty] = Field(default_factory=dict)


class RunSnapshot(ContractModel):
    schema_version: Literal["0.1"] = "0.1"
    scenario: ScenarioSpec
    customers: tuple[CustomerSpec, ...]
    serving_profiles: tuple[ServingProfile, ...]
    worker_groups: tuple[WorkerGroupSpec, ...]
    datasets: tuple[DatasetManifest, ...]
    bindings: tuple[ServingBinding, ...]
    versions: ExecutionVersions = Field(default_factory=ExecutionVersions)
    inventory: InfrastructureInventory | None = None
    admission_schedule: Artifact | None = None
    target: ExecutionTarget | None = None
    preparation: tuple[PreparationManifest, ...] = ()
    delivery_policy: DeliveryPolicy = Field(default_factory=DeliveryPolicy)
    derivation: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_relationships(self) -> Self:
        from .validation import validate_snapshot

        validate_snapshot(self)
        return self


class RequestCounts(ContractModel):
    offered: Count
    attempted: Count
    eligible_for_slo: Count
    completed: Count
    rejected: Count
    failed: Count
    timed_out: Count
    cancelled: Count
    not_sent: Count = 0

    @model_validator(mode="after")
    def check_partition(self) -> Self:
        if self.offered != self.attempted + self.not_sent:
            raise ValueError("offered requests must equal attempted plus not_sent")
        if self.attempted != sum(
            (self.completed, self.rejected, self.failed, self.timed_out, self.cancelled)
        ):
            raise ValueError("terminal request outcomes must partition attempted requests")
        if self.eligible_for_slo > self.attempted:
            raise ValueError("eligible requests exceed attempted requests")
        return self


class SLOWindow(ContractModel):
    offset_s: Count
    duration_s: PositiveInt
    eligible_requests: Count
    good_requests: Count
    verdict: SLOVerdict

    @property
    def attainment_fraction(self) -> float | None:
        if not self.eligible_requests:
            return None
        return self.good_requests / self.eligible_requests

    @model_validator(mode="after")
    def check_counts(self) -> Self:
        if self.good_requests > self.eligible_requests:
            raise ValueError("good requests exceed eligible requests")
        return self


class SLOAssessment(ContractModel):
    customer_ref: RevisionRef
    metric: LatencyMetric
    threshold_ms: PositiveNumber
    minimum_attainment_fraction: Annotated[Fraction, Field(gt=0)]
    minimum_eligible_requests: PositiveInt
    windows: Annotated[tuple[SLOWindow, ...], Field(min_length=1)]
    verdict: SLOVerdict

    @model_validator(mode="after")
    def validate_supplied_verdicts(self) -> Self:
        """Check supplied aggregates, not raw request latency or eligibility decisions."""
        previous_end = 0
        for window in self.windows:
            if window.offset_s < previous_end:
                raise ValueError("SLO windows overlap or are unordered")
            previous_end = window.offset_s + window.duration_s
            if window.eligible_requests < self.minimum_eligible_requests:
                expected = SLOVerdict.INSUFFICIENT_DATA
            elif window.attainment_fraction >= self.minimum_attainment_fraction:
                expected = SLOVerdict.PASSING
            else:
                expected = SLOVerdict.VIOLATING
            if window.verdict != expected:
                raise ValueError("SLO window verdict disagrees with its counts and target")
        expected_overall = (
            SLOVerdict.VIOLATING
            if any(w.verdict == "violating" for w in self.windows)
            else SLOVerdict.INSUFFICIENT_DATA
            if any(w.verdict == "insufficient_data" for w in self.windows)
            else SLOVerdict.PASSING
        )
        if self.verdict != expected_overall:
            raise ValueError("SLO assessment verdict disagrees with its windows")
        return self


class LatencySummary(ContractModel):
    sample_count: PositiveInt
    p50_ms: Milliseconds
    p95_ms: Milliseconds
    p99_ms: Milliseconds

    @model_validator(mode="after")
    def ordered_quantiles(self) -> Self:
        if not self.p50_ms <= self.p95_ms <= self.p99_ms:
            raise ValueError("latency quantiles must be ordered")
        return self


class CustomerMeasurements(ContractModel):
    customer_ref: RevisionRef
    input_tokens: Count | None = None
    cached_input_tokens: Count | None = None
    output_tokens: Count | None = None
    latencies: dict[LatencyMetric, LatencySummary] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_cached_tokens(self) -> Self:
        if (
            self.cached_input_tokens is not None
            and self.input_tokens is not None
            and self.cached_input_tokens > self.input_tokens
        ):
            raise ValueError("cached input tokens exceed input tokens")
        return self


class QualityResult(ContractModel):
    customer_ref: RevisionRef
    evaluator: NonEmpty
    status: Literal["measured", "not_evaluated"]
    scores: dict[NonEmpty, Annotated[float, Field(strict=True)]] = Field(default_factory=dict)
    notes: NonEmpty

    @model_validator(mode="after")
    def check_scores(self) -> Self:
        if self.status == "not_evaluated" and self.scores:
            raise ValueError("unevaluated quality cannot claim scores")
        if self.status == "measured" and not self.scores:
            raise ValueError("measured quality requires scores")
        return self


class RunResult(ContractModel):
    validity: Literal["valid", "invalid"]
    invalid_reasons: tuple[NonEmpty, ...] = ()
    requests: RequestCounts
    customer_requests: dict[RevisionRef, RequestCounts]
    customer_sessions: dict[RevisionRef, SessionCounts] = Field(default_factory=dict)
    measurements: tuple[CustomerMeasurements, ...] = ()
    slo_assessments: tuple[SLOAssessment, ...] = ()
    slo_verdict: SLOVerdict
    quality: tuple[QualityResult, ...] = ()
    artifacts: dict[Slug, Artifact] = Field(default_factory=dict)
    notes: NonEmpty | None = None

    @model_validator(mode="after")
    def check_result(self) -> Self:
        for name in RequestCounts.model_fields:
            if getattr(self.requests, name) != sum(
                getattr(counts, name) for counts in self.customer_requests.values()
            ):
                raise ValueError("aggregate request counts disagree with per-customer counts")
        if self.requests.not_sent and self.validity == "valid":
            raise ValueError("an incomplete offered schedule requires invalid measurements")
        if self.validity == "invalid":
            if not self.invalid_reasons:
                raise ValueError("invalid measurements require reasons")
            if self.slo_assessments or self.slo_verdict != SLOVerdict.NOT_EVALUATED:
                raise ValueError("invalid measurements cannot produce SLO assessments")
        else:
            if self.invalid_reasons:
                raise ValueError("valid measurements cannot have invalidity reasons")
            expected = (
                SLOVerdict.NOT_EVALUATED
                if not self.slo_assessments
                else SLOVerdict.VIOLATING
                if any(a.verdict == "violating" for a in self.slo_assessments)
                else SLOVerdict.INSUFFICIENT_DATA
                if any(a.verdict == "insufficient_data" for a in self.slo_assessments)
                else SLOVerdict.PASSING
            )
            if self.slo_verdict != expected:
                raise ValueError("run SLO verdict disagrees with assessments")
        keys = [(a.customer_ref, a.metric) for a in self.slo_assessments]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate customer/metric SLO assessment")
        for records in (self.measurements, self.quality):
            if len({record.customer_ref for record in records}) != len(records):
                raise ValueError("duplicate customer measurements or quality results")
        return self


class RunnerObservation(ContractModel):
    status: ExecutionStatus
    stage: Literal["preparing", "warming_up", "measuring", "draining", "finalizing"] | None = None
    result: RunResult | None = None
    error: ErrorInfo | None = None
    timeline_origin_at: Timestamp | None = None


class Run(RuntimeRecord):
    id: Slug
    execution_attempt: PositiveInt = 1
    status: ExecutionStatus
    stage: Literal["preparing", "warming_up", "measuring", "draining", "finalizing"] | None = None
    created_at: Timestamp
    started_at: Timestamp | None = None
    finished_at: Timestamp | None = None
    snapshot: RunSnapshot
    result: RunResult | None = None
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def check_execution(self) -> Self:
        validate_lifecycle(self.status, self.created_at, self.started_at, self.finished_at)
        if self.stage is not None and self.status != ExecutionStatus.RUNNING:
            raise ValueError("only running executions have an active stage")
        if self.status == ExecutionStatus.COMPLETED and self.result is None:
            raise ValueError("completed run requires a result")
        if self.status == ExecutionStatus.FAILED and self.error is None:
            raise ValueError("failed run requires an error")
        if self.status in {ExecutionStatus.QUEUED, ExecutionStatus.RUNNING} and self.result:
            raise ValueError("final result requires terminal execution")
        if self.status == ExecutionStatus.COMPLETED and self.error:
            raise ValueError("completed execution cannot carry an execution error")
        if self.result:
            from .validation import validate_run_result

            validate_run_result(self.snapshot, self.result)
        return self


def build_run_snapshot(
    catalog: ContractCatalog,
    scenario_ref: str,
    bindings: tuple[ServingBinding, ...],
    *,
    versions: ExecutionVersions | None = None,
    inventory: InfrastructureInventory | None = None,
    admission_schedule: Artifact | None = None,
) -> RunSnapshot:
    """Resolve once and serialize an independent, default-filled copy of all run inputs."""
    scenario = catalog.get("scenario", scenario_ref)
    customers = [catalog.get("customer", ref) for ref in scenario.participants]
    dataset_refs = {customer.workload.dataset_ref for customer in customers}
    dataset_refs.update(scenario.warmup.datasets.values())
    snapshot = RunSnapshot(
        scenario=scenario,
        customers=customers,
        serving_profiles=[
            catalog.get("serving-profile", ref)
            for ref in sorted({binding.serving_profile_ref for binding in bindings})
        ],
        worker_groups=[
            catalog.get("worker-group", ref)
            for ref in sorted({binding.worker_group_ref for binding in bindings})
        ],
        datasets=[catalog.get("dataset-manifest", ref) for ref in sorted(dataset_refs)],
        bindings=bindings,
        versions=versions or ExecutionVersions(),
        inventory=inventory,
        admission_schedule=admission_schedule,
    )
    return RunSnapshot.model_validate_json(snapshot.model_dump_json())
