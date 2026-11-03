"""Desired Lambda allocation is separate from observation and provisioning progress."""

from typing import Literal, Self

from pydantic import model_validator

from .common import (
    ContractModel,
    Count,
    ErrorInfo,
    GPUHardware,
    NonEmpty,
    PositiveInt,
    RevisionRef,
    RuntimeRecord,
    Slug,
    Timestamp,
    VersionedSpec,
)


class WorkerGroupSpec(VersionedSpec):
    provider: Literal["lambda"] = "lambda"
    instance_type: NonEmpty | None = None
    region: NonEmpty | None = None
    desired_instances: Count = 1
    hardware: GPUHardware
    worker_class: Slug
    host_profile: NonEmpty = "lambda-gpu-worker-v1"
    ttl_minutes: PositiveInt = 90
    drain_timeout_s: PositiveInt = 120


class WorkerMember(ContractModel):
    provider_instance_id: NonEmpty
    phase: Literal["allocated", "configuring", "joining", "gpu_ready", "failed", "terminated"]
    node_name: NonEmpty | None = None
    node_uid: NonEmpty | None = None
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def check_node_identity(self) -> Self:
        if (self.node_name is None) != (self.node_uid is None):
            raise ValueError("node name and UID must be supplied together")
        if self.phase == "gpu_ready" and self.node_uid is None:
            raise ValueError("GPU-ready member requires Kubernetes node identity")
        if self.phase == "failed" and self.error is None:
            raise ValueError("failed member requires an error")
        return self


class WorkerCounts(ContractModel):
    desired_instances: Count
    allocated_instances: Count
    joined_nodes: Count
    gpu_ready_nodes: Count


class WorkerGroupStatus(ContractModel):
    worker_group_ref: RevisionRef
    phase: Literal[
        "allocating",
        "joining",
        "ready",
        "partial_failure",
        "draining",
        "expired",
        "deleted",
        "failed",
    ]
    counts: WorkerCounts
    members: tuple[WorkerMember, ...] = ()
    observed_at: Timestamp
    allocated_at: Timestamp | None = None
    expires_at: Timestamp | None = None
    active_operation_id: Slug | None = None

    @model_validator(mode="after")
    def check_observation(self) -> Self:
        if len({member.provider_instance_id for member in self.members}) != len(self.members):
            raise ValueError("duplicate provider instance identity")
        active = [member for member in self.members if member.phase != "terminated"]
        expected = (
            len(active),
            sum(m.node_uid is not None for m in active),
            sum(m.phase == "gpu_ready" for m in active),
        )
        observed = (
            self.counts.allocated_instances,
            self.counts.joined_nodes,
            self.counts.gpu_ready_nodes,
        )
        if expected != observed:
            raise ValueError("worker counts disagree with members")
        if self.phase == "ready" and self.counts.gpu_ready_nodes != self.counts.desired_instances:
            raise ValueError("ready group has not reached its desired count")
        if (self.allocated_at is None) != (self.expires_at is None):
            raise ValueError("allocation and expiry timestamps must be supplied together")
        if self.members and self.allocated_at is None:
            raise ValueError("allocated members require a recorded lease deadline")
        if self.allocated_at is not None:
            if self.expires_at <= self.allocated_at or self.observed_at < self.allocated_at:
                raise ValueError("invalid allocation, observation, or expiry ordering")
        if self.phase == "deleted" and active:
            raise ValueError("deleted status requires confirmed termination")
        if self.phase == "expired":
            if active or self.expires_at is None or self.observed_at < self.expires_at:
                raise ValueError(
                    "expired status requires deadline reached and confirmed termination"
                )
        return self


class WorkerGroup(RuntimeRecord):
    spec: WorkerGroupSpec
    status: WorkerGroupStatus

    @model_validator(mode="after")
    def match_spec(self) -> Self:
        if self.spec.ref != self.status.worker_group_ref:
            raise ValueError("worker status references a different spec")
        if self.spec.desired_instances != self.status.counts.desired_instances:
            raise ValueError("worker status desired count differs from spec")
        return self
