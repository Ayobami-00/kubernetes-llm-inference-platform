"""Bounded state-machine steps with durable checkpoints and transaction-free side effects."""

import copy
import logging
import random
import signal
import threading
from datetime import datetime, timedelta
from uuid import uuid4

from platform_contracts import OperationStatus, Run, RunSnapshot, WorkerGroup
from sqlalchemy import select, text, update

from platform_management.db.models import (
    Event,
    Job,
    Operation,
    ProviderIntent,
    Resource,
    RunRow,
    SimInstance,
    WorkerHeartbeat,
)
from platform_management.db.session import engine, session
from platform_management.integrations import registry
from platform_management.jobs.external_runner import acknowledge_finished, external_step
from platform_management.jobs.queue import claim, owned
from platform_management.jobs.reconciliation import reconcile_expiry, refresh_capacity
from platform_management.providers.base import (
    AllocationAmbiguous,
    CapacityUnavailable,
    TransientProviderError,
)
from platform_management.providers.lambda_mutations import (
    ProviderIdentityConflict,
    ProviderRejected,
)
from platform_management.services.common import AppError, lock_key, now
from platform_management.settings import get_settings

log = logging.getLogger(__name__)


def stamp_operation(data, status=None, step=None, error=None, cleanup=None):
    data = copy.deepcopy(data)
    stamp = now().isoformat()
    if data["status"] == "queued":
        data.update(status="running", started_at=stamp)
    if status:
        data["status"] = status
        if status in {"completed", "failed", "cancelled"}:
            data["finished_at"] = data.get("finished_at") or stamp
    if step:
        data["current_step"] = step
    if error:
        data["error"] = error
    if cleanup is not None:
        data["cleanup_pending"] = cleanup
    data["updated_at"] = stamp
    return OperationStatus.model_validate(data).model_dump(mode="json")


def counts(group):
    status = group["status"]
    active = [m for m in status["members"] if m["phase"] != "terminated"]
    status["counts"] = {
        "desired_instances": group["spec"]["desired_instances"],
        "allocated_instances": len(active),
        "joined_nodes": sum(m["node_uid"] is not None for m in active),
        "gpu_ready_nodes": sum(m["phase"] == "gpu_ready" for m in active),
    }
    status["observed_at"] = now().isoformat()
    WorkerGroup.model_validate(group)


def step_group(op, payload, checkpoint, config=None):
    """Return a replacement resource payload and next checkpoint; provider calls have no DB tx."""
    config = config or registry.configuration()
    if config.get("mode") == "real":
        from platform_management.jobs.real_worker import step_real_group

        result = step_real_group(op, payload, checkpoint, config)
        counts(result[0]["group"])
        return result
    payload = copy.deepcopy(payload)
    group, fault = payload["group"], payload["fault"]
    state, spec = group["status"], group["spec"]
    phase = checkpoint.get(
        "step",
        "allocate"
        if op["type"] == "create_worker_group"
        else "renew"
        if op["type"] == "renew_worker_group"
        else "drain",
    )
    request = checkpoint["request"]
    if (
        op["type"] == "create_worker_group"
        and state.get("expires_at")
        and datetime.fromisoformat(state["expires_at"]) <= now()
        and phase not in {"drain", "terminate", "cleanup"}
    ):
        checkpoint.update(step="drain", creation_failed=True)
        request["expiry"] = True
        phase = "drain"
    done, failed = False, checkpoint.get("creation_failed", False)
    provider = registry.simulated(config, "provider")
    if phase == "allocate":
        with session() as db, db.begin():
            if not db.get(ProviderIntent, op["id"]):
                db.add(
                    ProviderIntent(
                        id=op["id"],
                        group_id=payload["id"],
                        payload={"desired": spec["desired_instances"], "fault": fault},
                    )
                )
        allocated = provider.reconcile(op["id"])
        if allocated is None:
            raise AllocationAmbiguous("Allocation remains unresolved; reconciliation required")
        if not allocated:
            allocated = provider.allocate(op["id"])
        if allocated:
            first = min(datetime.fromisoformat(i["allocated_at"]) for i in allocated)
            state["allocated_at"] = state["allocated_at"] or first.isoformat()
            state["expires_at"] = (
                state["expires_at"] or (first + timedelta(minutes=spec["ttl_minutes"])).isoformat()
            )
        state["members"] = [
            {
                "provider_instance_id": i["id"],
                "phase": "allocated",
                "node_name": None,
                "node_uid": None,
                "error": None,
            }
            for i in allocated
        ]
        checkpoint["step"] = "configure"
    elif phase == "configure":
        if fault == "partial":
            for member in state["members"]:
                member.update(
                    phase="failed",
                    error={
                        "code": "bootstrap_failed",
                        "message": "Synthetic partial allocation/bootstrap failure",
                    },
                )
            state["phase"] = "partial_failure"
            checkpoint.update(step="drain", creation_failed=True)
        else:
            for member in state["members"]:
                registry.simulated(config, "host").prepare(
                    member["provider_instance_id"], spec["host_profile"], op["id"]
                )
                member["phase"] = "configuring"
            checkpoint["step"] = "join"
    elif phase == "join":
        state["phase"] = "joining"
        for member in state["members"]:
            name, uid = registry.simulated(config, "cluster").join(
                member["provider_instance_id"], op["id"]
            )
            member.update(phase="joining", node_name=name, node_uid=uid)
        checkpoint["step"] = "gpu_readiness"
    elif phase == "gpu_readiness":
        for member in state["members"]:
            member["phase"] = "gpu_ready"
        state["phase"] = "ready"
        done = True
    elif phase == "renew":
        if (
            state["phase"] in {"draining", "expired", "deleted"}
            or not state["expires_at"]
            or datetime.fromisoformat(state["expires_at"]) <= now()
        ):
            raise CapacityUnavailable("The lease can no longer be renewed")
        state["expires_at"] = (
            datetime.fromisoformat(state["expires_at"])
            + timedelta(minutes=request["additional_minutes"])
        ).isoformat()
        done = True
    elif phase == "drain":
        state["phase"] = "draining"
        for member in state["members"]:
            if member["node_uid"]:
                registry.simulated(config, "cluster").drain(
                    member["node_uid"], spec["drain_timeout_s"], op["id"]
                )
        checkpoint["step"] = "terminate"
    elif phase == "terminate":
        # Reconcile the provider ledger too: a previous response could have been lost.
        with session() as db:
            intents = list(
                db.scalars(
                    select(ProviderIntent.id).where(ProviderIntent.group_id == payload["id"])
                )
            )
        identities = {m["provider_instance_id"] for m in state["members"]}
        for intent in intents:
            observation = provider.reconcile(intent)
            if observation is None:
                raise AllocationAmbiguous("Allocation identities remain unresolved")
            for instance in observation:
                if instance["id"] not in identities:
                    state["members"].append(
                        {
                            "provider_instance_id": instance["id"],
                            "phase": "allocated",
                            "node_name": None,
                            "node_uid": None,
                            "error": None,
                        }
                    )
                    identities.add(instance["id"])
                    if not state["allocated_at"]:
                        first = datetime.fromisoformat(instance["allocated_at"])
                        state["allocated_at"] = first.isoformat()
                        state["expires_at"] = (
                            first + timedelta(minutes=spec["ttl_minutes"])
                        ).isoformat()
        for member in state["members"]:
            provider.terminate(member["provider_instance_id"], fault)
            if not provider.termination_confirmed(member["provider_instance_id"]):
                raise TransientProviderError("Termination remains unconfirmed")
            member["phase"] = "terminated"
        checkpoint["step"] = "cleanup"
    elif phase == "cleanup":
        for member in state["members"]:
            registry.simulated(config, "host").remove(member["provider_instance_id"], op["id"])
        state["phase"] = "expired" if request.get("expiry") else "failed" if failed else "deleted"
        done = True
    counts(group)
    return payload, checkpoint, done, checkpoint.get("creation_failed", False), phase


def step_run(op, data, context, checkpoint, config=None, owner=None):
    data, context = copy.deepcopy(data), copy.deepcopy(context)
    config = config or registry.configuration()
    if config.get("runner") != "simulated:v1":
        return external_step(op, data, context, checkpoint, config, owner)
    phase = checkpoint.get("index", 0)
    runner = registry.runner(config, data["id"], context.get("outcome", "passing"))
    if op["type"] == "stop_run" or context.get("stop_requested"):
        if data["status"] not in {"completed", "failed", "cancelled"}:
            runner.stop(context.get("handle", ""))
            data.update(status="cancelled", stage=None, finished_at=now().isoformat())
        return data, context, checkpoint, True, "stop runner"
    if data["status"] in {"cancelled", "failed", "completed"}:
        return data, context, checkpoint, True, "observe terminal attempt"
    with session() as db:
        for identity in context.get("group_ids", {}).values():
            group = db.get(Resource, identity)
            state = group.payload["group"]["status"] if group else {}
            if state.get("phase") != "ready" or (
                state.get("expires_at") and datetime.fromisoformat(state["expires_at"]) <= now()
            ):
                raise CapacityUnavailable("A selected worker group is no longer ready")
    snapshot = RunSnapshot.model_validate(data["snapshot"])
    stages = ["preparing", "warming_up", "measuring", "draining", "finalizing"]
    if phase == 0:
        context["handle"] = runner.launch(snapshot, op["id"])
        data.update(status="running", started_at=now().isoformat())
    if phase < len(stages):
        data["stage"] = stages[phase]
        checkpoint["index"] = phase + 1
        return data, context, checkpoint, False, stages[phase]
    observation = runner.observe(context["handle"])
    if observation.status in {"queued", "running"}:
        return data, context, checkpoint, False, observation.stage or "observing runner"
    data.update(
        status=observation.status,
        stage=None,
        finished_at=now().isoformat(),
        result=observation.result.model_dump(mode="json") if observation.result else None,
        error=observation.error.model_dump(mode="json") if observation.error else None,
    )
    return (
        Run.model_validate(data).model_dump(mode="json"),
        context,
        checkpoint,
        True,
        "finalized",
    )


def execute(identity, owner):
    # A dedicated autocommit connection holds the session lock, never a long transaction.
    with session() as db:
        job = db.get(Job, identity)
        internal = job.kind != "operation"
        config = registry.binding(db, job.binding_id) if not internal else None
        target = db.get(Operation, identity).target_id if not internal else identity
    if internal:
        from platform_management.services.imports import execute_import

        return execute_import(identity, owner)
    key = lock_key("execution:" + target)
    with engine().connect().execution_options(isolation_level="AUTOCOMMIT") as guard:
        if not guard.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}):
            with session() as db, db.begin():
                job = owned(db, identity, owner)
                job.state, job.owner = "pending", None
                job.due_at = now() + timedelta(seconds=1)
            return
        stop_heartbeat = threading.Event()

        def heartbeat():
            while not stop_heartbeat.wait(get_settings().heartbeat_seconds):
                with session() as db, db.begin():
                    db.execute(
                        update(Job)
                        .where(Job.id == identity, Job.owner == owner, Job.state == "claimed")
                        .values(lease_until=now() + timedelta(seconds=get_settings().lease_seconds))
                    )

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            with session() as db, db.begin():
                job = owned(db, identity, owner)
                checkpoint = copy.deepcopy(job.checkpoint)
                operation = db.get(Operation, identity)
                op = stamp_operation(operation.payload, step="Resuming durable workflow")
                operation.payload = op
                resource = db.get(Resource, target)
                run = db.get(RunRow, target)
                resource_data = copy.deepcopy(resource.payload) if resource else None
                run_data, run_context = (
                    (copy.deepcopy(run.payload), copy.deepcopy(run.context))
                    if run
                    else (None, None)
                )
            action = op["type"]
            if action.endswith("worker_group"):
                result, checkpoint, done, creation_failed, step = step_group(
                    op, resource_data, checkpoint, config
                )
            elif action.endswith("run"):
                run_data, run_context, checkpoint, done, step = step_run(
                    op, run_data, run_context, checkpoint, config, owner
                )
                result, creation_failed = None, False
            elif action.endswith("experiment"):
                from platform_management.jobs.a10_experiment import step_experiment

                result, checkpoint, done, creation_failed, step = step_experiment(
                    op, resource_data, checkpoint, config, owner
                )
            else:
                result = resource_data
                with session() as db:
                    group = db.get(Resource, result["group_id"])
                    ready = group and group.payload["group"]["status"]["phase"] == "ready"
                if not ready:
                    raise CapacityUnavailable("Worker group is not GPU-ready")
                adapter = registry.simulated(config, "serving")
                from platform_contracts import ServingProfile

                handle = adapter.deploy(
                    target, ServingProfile.model_validate(result["profile"]), identity
                )
                result["status"] = adapter.observe(handle)
                done, creation_failed, step = True, False, "Serving ready · simulated"
            with session() as db, db.begin():
                job = owned(db, identity, owner)
                operation = db.get(Operation, identity, with_for_update=True)
                resource = db.get(Resource, target, with_for_update=True)
                if result is not None:
                    if resource.kind == "experiment":
                        result["stop_requested"] = bool(
                            result.get("stop_requested") or resource.payload.get("stop_requested")
                        )
                        result["children"] = {
                            **result.get("children", {}),
                            **resource.payload.get("children", {}),
                        }
                    if done and "group" in result:
                        result["group"]["status"]["active_operation_id"] = None
                    resource.payload = result
                    if done:
                        resource.active_operation_id = None
                    if (
                        done
                        and "group" in result
                        and result["group"]["status"]["phase"] in {"expired", "deleted", "failed"}
                    ):
                        for previous in db.scalars(
                            select(Operation).where(Operation.target_id == target)
                        ):
                            if previous.id != identity and previous.payload.get("cleanup_pending"):
                                previous.payload = stamp_operation(previous.payload, cleanup=False)
                                db.add(
                                    Event(
                                        operation_id=previous.id,
                                        message=f"Cleanup confirmed by {identity}",
                                    )
                                )
                        for deployment in db.scalars(
                            select(Resource).where(Resource.kind == "deployment").with_for_update()
                        ):
                            if deployment.payload["group_id"] == target:
                                deployment.payload = {**deployment.payload, "status": "unavailable"}
                if run_data is not None:
                    row = db.get(RunRow, target, with_for_update=True)
                    if row.payload["status"] not in {"completed", "failed", "cancelled"}:
                        row.payload = Run.model_validate(run_data).model_dump(mode="json")
                        row.context = {
                            **row.context,
                            **run_context,
                            "stop_requested": bool(
                                row.context.get("stop_requested")
                                or run_context.get("stop_requested")
                            ),
                        }
                if creation_failed:
                    operation.payload = stamp_operation(
                        operation.payload,
                        status="failed",
                        step=step,
                        error=checkpoint.get("failure")
                        or {
                            "code": "lease_expired"
                            if checkpoint["request"].get("expiry")
                            else "partial_failure",
                            "message": "Lease expired during preparation"
                            if checkpoint["request"].get("expiry")
                            else "Synthetic partial failure",
                        },
                        cleanup=not done,
                    )
                elif done and action == "start_run" and run_data["status"] == "failed":
                    operation.payload = stamp_operation(
                        operation.payload,
                        status="failed",
                        step=step,
                        error=run_data["error"],
                        cleanup=False,
                    )
                elif done:
                    final = (
                        "cancelled"
                        if (action == "start_run" and run_data["status"] == "cancelled")
                        or (action.endswith("experiment") and result["status"] == "cancelled")
                        else "completed"
                    )
                    operation.payload = stamp_operation(
                        operation.payload, status=final, step=step, cleanup=False
                    )
                else:
                    operation.payload = stamp_operation(
                        operation.payload, step=step, cleanup=action == "delete_worker_group"
                    )
                if done and run_data is not None and action == "stop_run":
                    for prior in db.scalars(select(Operation).where(Operation.target_id == target)):
                        if prior.id != identity and prior.payload.get("cleanup_pending"):
                            prior.payload = stamp_operation(prior.payload, cleanup=False)
                job.checkpoint, job.state, job.owner = (
                    checkpoint,
                    "completed" if done else "pending",
                    None,
                )
                job.attempt = 0
                job.due_at = now() + timedelta(
                    seconds=5
                    if config.get("provider") == "lambda:v1"
                    else get_settings().simulation_step_seconds
                )
                db.add(Event(operation_id=identity, message=step.replace("_", " ")))
        except Exception as exc:
            permanent = isinstance(
                exc,
                (
                    CapacityUnavailable,
                    ValueError,
                    AppError,
                    ProviderIdentityConflict,
                    ProviderRejected,
                ),
            )
            with session() as db, db.begin():
                job = owned(db, identity, owner)
                job.attempt += 1
                terminal = permanent or job.attempt >= 5
                operation = db.get(Operation, identity, with_for_update=True)
                resource = db.get(Resource, target, with_for_update=True)
                cleanup = False
                if resource and resource.kind == "experiment" and action == "start_experiment":
                    # Retry reconciliation/cleanup, never replay an execution after an error.
                    # Keep the reservation until existing children are terminal.
                    if terminal:
                        error = {
                            "code": "experiment_failed",
                            "message": "Experiment control failed; reconciling retained children",
                        }
                        job.checkpoint = {**job.checkpoint, "failure": error, "step": "cleanup"}
                        resource.payload = {**resource.payload, "error": error, "stage": "cleanup"}
                        terminal = False
                if resource and resource.kind == "worker_group":
                    payload = copy.deepcopy(resource.payload)
                    status = payload["group"]["status"]
                    # Recover confirmed simulator side effects even if a checkpoint write failed.
                    ledger = list(
                        db.scalars(select(SimInstance).where(SimInstance.group_id == target))
                    )
                    members = {
                        member["provider_instance_id"]: member for member in status["members"]
                    }
                    for instance in ledger:
                        if instance.id not in members:
                            member = {
                                "provider_instance_id": instance.id,
                                "phase": "allocated",
                                "node_name": None,
                                "node_uid": None,
                                "error": None,
                            }
                            status["members"].append(member)
                            members[instance.id] = member
                        if instance.state == "terminated":
                            members[instance.id]["phase"] = "terminated"
                    if ledger and not status["allocated_at"]:
                        first = min(instance.allocated_at for instance in ledger)
                        status["allocated_at"] = first.isoformat()
                        status["expires_at"] = (
                            first + timedelta(minutes=payload["group"]["spec"]["ttl_minutes"])
                        ).isoformat()
                    cleanup = any(m["phase"] != "terminated" for m in status["members"])
                    unresolved = db.scalar(
                        select(ProviderIntent).where(
                            ProviderIntent.group_id == target, ProviderIntent.state == "unknown"
                        )
                    )
                    cleanup = cleanup or unresolved is not None
                    if payload.get("mode") == "real":
                        from platform_management.jobs.real_worker import recover_real_group

                        cleanup = recover_real_group(db, payload) or cleanup
                    if terminal:
                        status["phase"] = "draining" if cleanup else "failed"
                        status["active_operation_id"] = None
                        resource.active_operation_id = None
                    counts(payload["group"])
                    resource.payload = payload
                if terminal and resource and resource.kind == "deployment":
                    resource.payload = {**resource.payload, "status": "failed"}
                    resource.active_operation_id = None
                if terminal and run_data:
                    persisted_run = db.get(RunRow, target, with_for_update=True)
                    if persisted_run.context.get("handle") and config.get("runner", "").startswith(
                        "kubernetes"
                    ):
                        cleanup = True
                        persisted_run.context = {**persisted_run.context, "cleanup_pending": True}
                error = {
                    "code": "provider_failure"
                    if isinstance(exc, (CapacityUnavailable, TransientProviderError))
                    else "workflow_failure",
                    "message": str(exc)[:500],
                }
                operation.payload = stamp_operation(
                    operation.payload,
                    status="failed" if terminal else None,
                    step="Cleanup pending" if terminal and cleanup else "Retry scheduled",
                    error=error if terminal else None,
                    cleanup=cleanup,
                )
                job.state, job.owner = ("failed" if terminal else "pending"), None
                job.due_at = now() + timedelta(seconds=min(30, 2**job.attempt) + random.random())
                if (
                    terminal
                    and run_data
                    and not cleanup
                    and run_data["status"] not in {"completed", "failed", "cancelled"}
                ):
                    row = db.get(RunRow, target)
                    value = copy.deepcopy(row.payload)
                    value.update(
                        status="failed", stage=None, finished_at=now().isoformat(), error=error
                    )
                    row.payload = Run.model_validate(value).model_dump(mode="json")
                db.add(
                    Event(
                        operation_id=identity,
                        message=f"{error['message']} · attempt {job.attempt}/5",
                    )
                )
            log.warning("Workflow %s: %s", identity, type(exc).__name__)
        finally:
            stop_heartbeat.set()
            thread.join(timeout=2)
            guard.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})


def tick():
    reconcile_expiry()
    acknowledge_finished()
    owner = uuid4().hex
    with session() as db, db.begin():
        identity = claim(db, owner, get_settings().lease_seconds)
    if identity:
        execute(identity, owner)
    return identity


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    last_refresh = 0
    last_heartbeat = 0
    import os

    worker_id = os.environ.get("HOSTNAME", "local-worker")
    import time

    while not stop.is_set():
        try:
            if time.monotonic() - last_heartbeat > 10:
                with session() as db, db.begin():
                    db.merge(WorkerHeartbeat(id=worker_id, observed_at=now()))
                last_heartbeat = time.monotonic()
            if time.monotonic() - last_refresh > 30:
                refresh_capacity()
                from platform_management.integrations.observations import refresh_cluster

                refresh_cluster()
                last_refresh = time.monotonic()
            identity = tick()
            if not identity:
                stop.wait(0.25)
        except AppError:
            log.warning("Claim ownership changed during recovery")
        except Exception:
            log.exception("Worker loop failed; retrying without losing persisted claims")
            stop.wait(1)


if __name__ == "__main__":
    main()
