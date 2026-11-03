"""Public action progress; durable claims, retries and checkpoints belong to the backend."""

from typing import Literal, Self

from pydantic import model_validator

from .common import (
    ContractModel,
    ErrorInfo,
    ExecutionStatus,
    NonEmpty,
    PositiveInt,
    RuntimeRecord,
    Slug,
    Timestamp,
    validate_lifecycle,
)


class OperationTarget(ContractModel):
    resource_type: Literal["worker_group", "serving_deployment", "run"]
    resource_id: Slug
    requested_revision: PositiveInt | None = None


class OperationStatus(RuntimeRecord):
    id: Slug
    type: Literal[
        "create_worker_group",
        "delete_worker_group",
        "renew_worker_group",
        "deploy_serving",
        "start_run",
        "stop_run",
    ]
    target: OperationTarget
    status: ExecutionStatus
    current_step: NonEmpty | None = None
    created_at: Timestamp
    started_at: Timestamp | None = None
    updated_at: Timestamp
    finished_at: Timestamp | None = None
    error: ErrorInfo | None = None
    cleanup_pending: bool = False

    @model_validator(mode="after")
    def check_state(self) -> Self:
        validate_lifecycle(self.status, self.created_at, self.started_at, self.finished_at)
        if self.updated_at < (self.finished_at or self.started_at or self.created_at):
            raise ValueError("updated_at precedes operation progress")
        if self.status == ExecutionStatus.FAILED and self.error is None:
            raise ValueError("failed operation requires an error")
        if self.status == ExecutionStatus.COMPLETED and (self.error or self.cleanup_pending):
            raise ValueError("completed operation cannot have an error or pending cleanup")
        if self.status in {ExecutionStatus.COMPLETED, ExecutionStatus.CANCELLED}:
            if self.cleanup_pending:
                raise ValueError("cleanup must finish before completing or cancelling an operation")
        expected_target = (
            "worker_group"
            if self.type.endswith("worker_group")
            else "serving_deployment"
            if self.type == "deploy_serving"
            else "run"
        )
        if self.target.resource_type != expected_target:
            raise ValueError("operation type and target resource disagree")
        return self
