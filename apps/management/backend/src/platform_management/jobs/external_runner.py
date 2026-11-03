"""Runner control retries observe a persisted attempt; they never restart its clock."""

import copy

from platform_contracts import Run, RunSnapshot
from sqlalchemy import select

from platform_management.db.models import RunRow
from platform_management.db.session import session
from platform_management.integrations import registry
from platform_management.jobs.queue import owned
from platform_management.services.common import now


def persist_context(run_id, operation_id, owner, values):
    with session() as db, db.begin():
        owned(db, operation_id, owner)
        row = db.get(RunRow, run_id, with_for_update=True)
        row.context = {**row.context, **values}


def external_step(op, data, context, checkpoint, config, owner):
    data, context = copy.deepcopy(data), copy.deepcopy(context)
    adapter = registry.runner(config, data["id"], context.get("outcome", "passing"))
    try:
        if data["status"] in {"completed", "failed", "cancelled"}:
            return data, context, checkpoint, True, "Historical terminal attempt"
        handle, uid = context.get("handle"), context.get("job_uid")
        if op["type"] == "stop_run" or context.get("stop_requested"):
            if handle:
                observation = adapter.observe(handle, uid) if uid else None
                if observation and observation.status == "completed":
                    data.update(
                        started_at=data.get("started_at")
                        or context.get("execution_started_at")
                        or data["created_at"],
                        status="completed",
                        stage=None,
                        result=observation.result.model_dump(mode="json"),
                        finished_at=now().isoformat(),
                    )
                elif not adapter.stop(handle, uid):
                    return data, context, checkpoint, False, "Runner cancellation · cleanup pending"
                else:
                    data.update(status="cancelled", stage=None, finished_at=now().isoformat())
            else:
                data.update(status="cancelled", stage=None, finished_at=now().isoformat())
            context.update(cleanup_pending=False, ack_pending=bool(uid))
            return (
                Run.model_validate(data).model_dump(mode="json"),
                context,
                checkpoint,
                True,
                "Runner stopped",
            )
        snapshot = RunSnapshot.model_validate(data["snapshot"])
        if not handle:
            handle = adapter.prepare(snapshot, op["id"])
            # Persist the intent BEFORE calling the API; an HTTP timeout is ambiguous.
            persist_context(
                data["id"],
                op["id"],
                owner,
                {"handle": handle, "job_uid": None, "execution_started_at": now().isoformat()},
            )
            context["handle"] = handle
            with session() as db:
                context["execution_started_at"] = db.get(RunRow, data["id"]).context[
                    "execution_started_at"
                ]
        if not uid:
            uid = adapter.ensure_started(handle)
            persist_context(data["id"], op["id"], owner, {"job_uid": uid})
            context["job_uid"] = uid
        if data["status"] == "queued":
            data.update(
                status="running",
                started_at=context.get("execution_started_at") or data["created_at"],
            )
        observation = adapter.observe(handle, uid)
        if observation.status in {"running", "queued"}:
            data["stage"] = observation.stage or "preparing"
            return data, context, checkpoint, False, data["stage"]
        data.update(
            status=observation.status,
            stage=None,
            finished_at=now().isoformat(),
            result=observation.result.model_dump(mode="json") if observation.result else None,
            error=observation.error.model_dump(mode="json") if observation.error else None,
        )
        context["ack_pending"] = True
        return (
            Run.model_validate(data).model_dump(mode="json"),
            context,
            checkpoint,
            True,
            "Runner result validated",
        )
    finally:
        adapter.close()


def acknowledge_finished():
    # Keep Jobs/logs until their result and terminal status are committed.
    with session() as db:
        candidates = [
            (r.id, copy.deepcopy(r.context), r.binding_id)
            for r in db.scalars(select(RunRow).where(RunRow.context["ack_pending"].as_boolean()))
        ]
    for run_id, context, binding_id in candidates:
        with session() as db:
            config = registry.binding(db, binding_id)
        adapter = registry.runner(config, run_id)
        try:
            adapter.acknowledge(context["handle"], context["job_uid"])
            with session() as db, db.begin():
                row = db.get(RunRow, run_id, with_for_update=True)
                row.context = {**row.context, "ack_pending": False}
        except Exception:
            # A retained Job is safe. Retry cleanup on the next worker tick.
            continue
        finally:
            adapter.close()
