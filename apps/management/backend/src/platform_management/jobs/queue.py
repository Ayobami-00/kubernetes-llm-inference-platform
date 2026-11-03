from datetime import timedelta

from platform_contracts import OperationStatus
from sqlalchemy import and_, or_, select, text

from platform_management.db.models import Event, Job, Operation, Submission
from platform_management.services.common import AppError, digest, identifier, lock_key, now


def submit(db, key, action, target, payload, mutate):
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key("submission:" + key)})
    fingerprint = digest({"action": action, "target": target, "payload": payload})
    previous = db.get(Submission, key)
    if previous:
        if previous.fingerprint != fingerprint:
            raise AppError(
                409, "Idempotency key was used for a different request", "idempotency_conflict"
            )
        operation = db.get(Operation, previous.operation_id)
        return {"operation_id": operation.id, "resource_id": operation.target_id}
    op_id = identifier("op")
    mutate(op_id)
    resource_type = (
        "worker_group"
        if action.endswith("worker_group")
        else "run"
        if action.endswith("run")
        else "serving_deployment"
    )
    stamp = now()
    op = OperationStatus(
        id=op_id,
        type=action,
        fixture=True,
        target={"resource_type": resource_type, "resource_id": target},
        status="queued",
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(Operation(id=op_id, target_id=target, payload=op.model_dump(mode="json")))
    db.flush()
    db.add(Job(id=op_id, checkpoint={"request": payload}, due_at=stamp))
    db.add(Submission(key=key, fingerprint=fingerprint, operation_id=op_id))
    db.add(Event(operation_id=op_id, message="Accepted · simulation mode"))
    return {"operation_id": op_id, "resource_id": target}


def claim(db, owner, lease_seconds=60):
    stamp = now()
    job = db.scalar(
        select(Job)
        .where(
            Job.due_at <= stamp,
            or_(Job.state == "pending", and_(Job.state == "claimed", Job.lease_until < stamp)),
        )
        .order_by(Job.due_at, Job.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if not job:
        return None
    job.owner = owner
    job.state = "claimed"
    job.lease_until = stamp + timedelta(seconds=lease_seconds)
    db.flush()
    return job.id


def owned(db, identity, owner):
    job = db.get(Job, identity, with_for_update=True)
    if job is None or job.owner != owner or job.state != "claimed" or job.lease_until <= now():
        raise AppError(409, "Worker no longer owns this execution claim", "stale_claim")
    return job
