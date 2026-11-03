"""Portable execution inputs and client observations; no engine or service dependencies."""

from typing import Annotated, Literal, Self
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, HttpUrl, JsonValue, StringConstraints, model_validator

from .common import (
    Artifact,
    ContractModel,
    Count,
    Fraction,
    NonEmpty,
    PositiveInt,
    RevisionRef,
    Sha256,
    Slug,
    Timestamp,
)

Milliseconds = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]


class ExecutionTarget(ContractModel):
    mode: Literal["local-stub", "benchmark"]
    endpoint: HttpUrl
    credential_env: dict[
        Slug, Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    ] = Field(default_factory=dict)
    stub_preset: Literal[
        "healthy",
        "slow-first",
        "slow-completion",
        "reject",
        "error",
        "timeout",
        "interrupted",
        "shared-overload",
    ] = "healthy"

    @model_validator(mode="after")
    def no_embedded_credentials(self) -> Self:
        url = urlsplit(str(self.endpoint))
        sensitive = {"token", "key", "api_key", "apikey", "authorization", "password", "signature"}
        if url.username or url.password or sensitive & {k.lower() for k in parse_qs(url.query)}:
            raise ValueError("endpoint credentials must use environment references, not URL values")
        if self.mode == "benchmark" and self.stub_preset != "healthy":
            raise ValueError("stub behavior presets are only available in local-stub mode")
        return self


class DeliveryPolicy(ContractModel):
    max_scheduling_lag_ms: Milliseconds = 100.0
    max_late_fraction: Fraction = 0.01


class PreparedTurn(ContractModel):
    messages: Annotated[tuple[dict[str, JsonValue], ...], Field(min_length=1)]
    tools: tuple[dict[str, JsonValue], ...] = ()
    source_tools: tuple[dict[str, JsonValue], ...] = ()
    controls: dict[str, JsonValue] = Field(default_factory=dict)
    source_controls: dict[str, JsonValue] = Field(default_factory=dict)
    references: dict[str, JsonValue] = Field(default_factory=dict)
    delay_ms: Milliseconds = 0.0
    input_tokens: Count
    adaptations: tuple[NonEmpty, ...] = ()


class PreparedEntry(ContractModel):
    id: Slug
    source_id: NonEmpty
    warmup: bool = False
    context_mode: Literal["recorded", "live"] = "recorded"
    turns: Annotated[tuple[PreparedTurn, ...], Field(min_length=1)]
    adaptations: tuple[NonEmpty, ...] = ()


class PreparationManifest(ContractModel):
    schema_version: Literal["0.1"] = "0.1"
    id: Slug
    customer_ref: RevisionRef
    source_manifest_refs: Annotated[tuple[RevisionRef, ...], Field(min_length=1)]
    data_kind: Literal["public", "synthetic"]
    source_revisions: dict[str, NonEmpty | None]
    source_urls: dict[str, NonEmpty]
    licenses: dict[str, NonEmpty | None]
    transform_version: NonEmpty
    tokenizer_id: NonEmpty
    tokenizer_revision: NonEmpty
    token_accounting: Literal["model_tokenizer", "emulated"]
    prompt_template_sha256: Sha256
    artifact: Artifact
    entry_count: PositiveInt
    warmup_count: Count
    max_rendered_input_tokens: Count
    scanned: Count
    downloaded_bytes: Count
    exclusions: dict[NonEmpty, Count] = Field(default_factory=dict)
    adaptations: dict[NonEmpty, Count] = Field(default_factory=dict)
    license_evidence: dict[str, NonEmpty] = Field(default_factory=dict)
    selection: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_manifest(self) -> Self:
        if len(set(self.source_manifest_refs)) != len(self.source_manifest_refs):
            raise ValueError("duplicate preparation source reference")
        if self.artifact.sha256 is None:
            raise ValueError("prepared inputs require a checksum")
        if self.warmup_count >= self.entry_count:
            raise ValueError("prepared inputs require scored examples")
        if self.scanned < self.entry_count:
            raise ValueError("selected inputs cannot exceed scanned source inputs")
        return self


class Admission(ContractModel):
    id: Slug
    customer_ref: RevisionRef
    entry_id: Slug
    scheduled_offset_ms: Milliseconds
    warmup: bool
    phase: Slug


class RequestRecord(ContractModel):
    schema_version: Literal["0.1"] = "0.1"
    run_id: Slug
    request_id: Slug
    customer_ref: RevisionRef
    source_entry_id: NonEmpty
    session_id: Slug
    turn_index: Count
    warmup_cohort: bool
    phase: Literal["warmup", "measurement", "drain"]
    scheduled_offset_ms: Milliseconds | None = None
    actual_send_offset_ms: Milliseconds | None = None
    completed_offset_ms: Milliseconds | None = None
    latency_ms: Milliseconds | None = None
    ttft_ms: Milliseconds | None = None
    tpot_ms: Milliseconds | None = None
    timing_source: Literal["aiperf_monotonic", "unavailable"] = "aiperf_monotonic"
    sent_at: Timestamp | None = None
    outcome: Literal["completed", "rejected", "failed", "timed_out", "cancelled", "not_sent"]
    eligible_for_slo: bool = False
    http_status: Count | None = None
    finish_reason: NonEmpty | None = None
    complete_output: bool = False
    input_tokens: Count | None = None
    output_tokens: Count | None = None
    cached_input_tokens: Count | None = None
    token_count_source: Literal["server", "tokenizer", "emulated", "unavailable"] = "unavailable"
    output_text: str | None = None
    tool_calls: tuple[dict[str, JsonValue], ...] = ()
    error: NonEmpty | None = None

    @model_validator(mode="after")
    def check_record(self) -> Self:
        if self.outcome == "not_sent":
            if self.actual_send_offset_ms is not None or self.eligible_for_slo:
                raise ValueError("unsent request cannot have a send time or be SLO eligible")
        elif self.actual_send_offset_ms is None:
            raise ValueError("attempted request requires dispatch timing")
        if self.completed_offset_ms is not None and self.actual_send_offset_ms is not None:
            if self.completed_offset_ms < self.actual_send_offset_ms:
                raise ValueError("request completed before dispatch")
        if self.warmup_cohort and self.eligible_for_slo:
            raise ValueError("warmup cohort cannot be SLO eligible")
        if (
            self.ttft_ms is not None
            and self.latency_ms is not None
            and self.ttft_ms > self.latency_ms
        ):
            raise ValueError("first output cannot follow completion")
        if self.complete_output and self.outcome != "completed":
            raise ValueError("only completed requests can have complete output")
        if self.cached_input_tokens is not None and self.input_tokens is not None:
            if self.cached_input_tokens > self.input_tokens:
                raise ValueError("cached input exceeds input tokens")
        return self


class SessionRecord(ContractModel):
    schema_version: Literal["0.1"] = "0.1"
    id: Slug
    customer_ref: RevisionRef
    source_entry_id: NonEmpty
    warmup_cohort: bool
    scheduled_offset_ms: Milliseconds
    started_offset_ms: Milliseconds | None
    ended_offset_ms: Milliseconds | None
    expected_turns: PositiveInt
    request_ids: tuple[Slug, ...]
    outcome: Literal["completed", "failed", "cancelled", "cutoff", "not_started"]
    reason: NonEmpty | None = None

    @model_validator(mode="after")
    def check_session(self) -> Self:
        if (self.started_offset_ms is None) != (self.outcome == "not_started"):
            raise ValueError("not_started must agree with session start timing")
        if len(set(self.request_ids)) != len(self.request_ids):
            raise ValueError("duplicate session request identity")
        if len(self.request_ids) > self.expected_turns:
            raise ValueError("session has too many requests")
        if self.outcome == "completed" and len(self.request_ids) != self.expected_turns:
            raise ValueError("completed session is missing turns")
        if self.started_offset_ms is not None and self.ended_offset_ms is not None:
            if self.ended_offset_ms < self.started_offset_ms:
                raise ValueError("session ended before it started")
        return self


class SessionCounts(ContractModel):
    started: Count = 0
    completed: Count = 0
    failed: Count = 0
    cancelled: Count = 0
    cutoff: Count = 0
    eligible_for_slo: Count = 0

    @model_validator(mode="after")
    def check_partition(self) -> Self:
        if self.started != self.completed + self.failed + self.cancelled + self.cutoff:
            raise ValueError("session outcomes must partition started sessions")
        if self.eligible_for_slo > self.started:
            raise ValueError("eligible sessions exceed started sessions")
        return self
