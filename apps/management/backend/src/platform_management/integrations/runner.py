from typing import Literal, Protocol

from platform_contracts import RunResult, RunSnapshot
from platform_contracts.common import ContractModel, ErrorInfo, ExecutionStatus
from sqlalchemy import select

from platform_management.db.models import RunRow
from platform_management.db.session import session
from platform_management.services.synthetic import result_for


class RunnerObservation(ContractModel):
    status: ExecutionStatus
    stage: Literal["preparing", "warming_up", "measuring", "draining", "finalizing"] | None = None
    result: RunResult | None = None
    error: ErrorInfo | None = None


class RunnerAdapter(Protocol):
    def launch(self, snapshot: RunSnapshot, operation_key: str) -> str:
        """Repeated calls with the same key must resolve the same execution attempt."""
        ...

    def stop(self, handle: str) -> None: ...
    def observe(self, handle: str) -> RunnerObservation: ...


class SimulatedRunner:
    """The durable worker advances this simulator; all state is in the persisted run attempt."""

    def launch(self, snapshot, operation_key):
        return f"simulated-{operation_key}"

    def stop(self, handle):
        if not handle:
            return
        with session() as db, db.begin():
            row = db.scalar(
                select(RunRow).where(RunRow.context["handle"].astext == handle).with_for_update()
            )
            if row:
                row.context = {**row.context, "stop_requested": True}

    def observe(self, handle):
        with session() as db:
            row = db.scalar(select(RunRow).where(RunRow.context["handle"].astext == handle))
            if row is None:
                return RunnerObservation(
                    status="failed",
                    error=ErrorInfo(
                        code="runner_lost",
                        message="The persisted simulated execution handle is missing",
                    ),
                )
            data, context = row.payload, row.context
            if data["status"] in {"completed", "failed", "cancelled"}:
                return RunnerObservation(
                    status=data["status"], result=data.get("result"), error=data.get("error")
                )
            if data.get("stage") != "finalizing":
                return RunnerObservation(status=data["status"], stage=data.get("stage"))
            if context["outcome"] == "failed":
                return RunnerObservation(
                    status="failed",
                    error=ErrorInfo(
                        code="simulated_runner_failure", message="Synthetic runner failure"
                    ),
                )
            return RunnerObservation(
                status="completed",
                result=result_for(RunSnapshot.model_validate(row.snapshot), context["outcome"]),
            )
