import copy
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response
from platform_contracts import Run, WorkerGroup
from sqlalchemy import select

from platform_management.api.dependencies import Db, administrator
from platform_management.api.schemas import (
    Accepted,
    Capacity,
    CreateGroup,
    DeploymentView,
    DeploymentWrite,
    Fault,
    GroupView,
    OperationView,
    Preflight,
    RenewGroup,
    RunLaunch,
    RunView,
    WorkerLifecycleOptions,
)
from platform_management.db.models import Event, Job, Observation, Operation, Resource, RunRow
from platform_management.jobs.queue import submit
from platform_management.jobs.reconciliation import refresh_capacity
from platform_management.services.catalog import catalog, snapshot
from platform_management.services.common import AppError, digest, now
from platform_management.services.readiness import assess

router = APIRouter(dependencies=[Depends(administrator)], tags=["Workflows"])
Idempotency = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)]


def resource(db, identity, kind):
    row = db.get(Resource, identity, with_for_update=True)
    if not row or row.kind != kind:
        raise AppError(404, "Resource not found")
    if row.active_operation_id:
        raise AppError(409, "A lifecycle operation is already active", "resource_busy")
    if row.payload.get("ownership") == "external":
        raise AppError(
            409,
            "B owns this resource; management lifecycle changes are disabled",
            "external_ownership",
        )
    return row


def accepted_headers(response, result):
    response.headers["Location"] = "/api/v1/operations/" + result["operation_id"]
    return result


@router.get("/worker-groups", response_model=list[GroupView], operation_id="listWorkerGroups")
def groups(db: Db):
    return list(
        db.scalars(
            select(Resource.payload).where(Resource.kind == "worker_group").order_by(Resource.id)
        )
    )


@router.get(
    "/worker-lifecycle-options",
    response_model=WorkerLifecycleOptions,
    operation_id="getWorkerLifecycleOptions",
)
def worker_lifecycle_options(db: Db):
    from platform_management.integrations.lambda_policy import checked_binding
    from platform_management.integrations.registry import configuration

    try:
        policy = checked_binding(configuration("real"))
    except AppError:
        return WorkerLifecycleOptions(
            reason="Live provisioning is not enabled for this environment."
        )
    compatible = []
    for reference, spec in sorted(catalog(db).records.get("worker-group", {}).items()):
        try:
            policy.check_group(spec)
        except AppError:
            continue
        compatible.append(
            {
                "spec_ref": reference,
                "worker_class": spec.worker_class,
                "lease_minutes": spec.ttl_minutes,
            }
        )
    return WorkerLifecycleOptions(
        configured=True,
        instance_type=policy.instance_type,
        region=policy.region,
        price_ceiling_cents_per_hour=policy.max_price_cents_per_hour,
        max_total_lease_minutes=policy.max_total_lease_minutes,
        compatible_specs=compatible,
        reason=None
        if compatible
        else "Publish a compatible A10 worker profile before provisioning.",
    )


@router.post(
    "/worker-groups", response_model=Accepted, status_code=202, operation_id="createWorkerGroup"
)
def create_group(body: CreateGroup, db: Db, response: Response, key: Idempotency):
    def mutate(op_id):
        if db.get(Resource, body.id):
            raise AppError(409, "Resource ID already exists")
        spec = catalog(db).get("worker-group", body.spec_ref)
        if body.mode == "real":
            from platform_management.integrations.lambda_policy import checked_binding
            from platform_management.integrations.registry import configuration

            if body.fault != "none":
                raise AppError(422, "Simulation faults cannot be used for a real worker")
            checked_binding(configuration("real")).check_group(spec)
        if spec.desired_instances == 0:
            raise AppError(422, "Allocation requires at least one desired instance")
        group = WorkerGroup(
            fixture=body.mode == "simulation",
            spec=spec,
            status={
                "worker_group_ref": spec.ref,
                "phase": "allocating",
                "counts": {
                    "desired_instances": spec.desired_instances,
                    "allocated_instances": 0,
                    "joined_nodes": 0,
                    "gpu_ready_nodes": 0,
                },
                "observed_at": now(),
                "active_operation_id": op_id,
            },
        )
        payload = GroupView(id=body.id, mode=body.mode, group=group, fault=body.fault).model_dump(
            mode="json"
        )
        db.add(
            Resource(id=body.id, kind="worker_group", payload=payload, active_operation_id=op_id)
        )

    return accepted_headers(
        response,
        submit(db, key, "create_worker_group", body.id, body.model_dump(mode="json"), mutate),
    )


@router.post(
    "/worker-groups/{identity}/renew",
    response_model=Accepted,
    status_code=202,
    operation_id="renewWorkerGroup",
)
def renew(identity: str, body: RenewGroup, db: Db, response: Response, key: Idempotency):
    def mutate(op_id):
        row = resource(db, identity, "worker_group")
        status = row.payload["group"]["status"]
        if (
            status["phase"] != "ready"
            or not status["expires_at"]
            or datetime.fromisoformat(status["expires_at"]) <= now()
        ):
            raise AppError(409, "Only a ready group with an active lease can be renewed")
        if row.payload.get("mode") == "real":
            from platform_management.integrations.lambda_policy import checked_binding
            from platform_management.integrations.registry import binding

            policy = checked_binding(binding(db, row.binding_id))
            requested_expiry = datetime.fromisoformat(status["expires_at"]) + timedelta(
                minutes=body.additional_minutes
            )
            limit = datetime.fromisoformat(status["allocated_at"]) + timedelta(
                minutes=policy.max_total_lease_minutes
            )
            if body.additional_minutes > 90 or requested_expiry > limit:
                raise AppError(
                    422, "Renewal exceeds the reviewed real lease limits", "capacity_policy"
                )
        row.active_operation_id = op_id
        payload = copy.deepcopy(row.payload)
        payload["group"]["status"]["active_operation_id"] = op_id
        row.payload = payload

    return accepted_headers(
        response, submit(db, key, "renew_worker_group", identity, body.model_dump(), mutate)
    )


@router.post(
    "/worker-groups/{identity}/delete",
    response_model=Accepted,
    status_code=202,
    operation_id="deleteWorkerGroup",
)
def delete(identity: str, db: Db, response: Response, key: Idempotency):
    def mutate(op_id):
        row = resource(db, identity, "worker_group")
        if row.payload["group"]["status"]["phase"] in {"deleted", "expired"}:
            raise AppError(409, "This resource is already terminated")
        row.active_operation_id = op_id
        payload = copy.deepcopy(row.payload)
        payload["group"]["status"]["active_operation_id"] = op_id
        row.payload = payload

    return accepted_headers(response, submit(db, key, "delete_worker_group", identity, {}, mutate))


@router.put(
    "/worker-groups/{identity}/simulation",
    response_model=GroupView,
    operation_id="setSimulationFault",
)
def fault(identity: str, fault: Fault, db: Db):
    row = resource(db, identity, "worker_group")
    if row.payload.get("mode") == "real":
        raise AppError(422, "Simulation faults cannot change real worker groups")
    row.payload = {**row.payload, "fault": fault}
    return row.payload


@router.get(
    "/serving-deployments",
    response_model=list[DeploymentView],
    operation_id="listServingDeployments",
)
def deployments(db: Db):
    return list(db.scalars(select(Resource.payload).where(Resource.kind == "deployment")))


@router.post(
    "/serving-deployments", response_model=Accepted, status_code=202, operation_id="deployServing"
)
def deploy(body: DeploymentWrite, db: Db, response: Response, key: Idempotency):
    def mutate(op_id):
        group = resource(db, body.group_id, "worker_group")
        if group.payload.get("mode", "simulation") != body.mode:
            raise AppError(422, "Serving and worker-group execution modes must match")
        if group.payload["group"]["status"]["phase"] != "ready":
            raise AppError(409, "Worker group must be GPU-ready")
        profile = catalog(db).get("serving-profile", body.profile_ref)
        spec = group.payload["group"]["spec"]
        if (
            profile.worker_class != spec["worker_class"]
            or profile.hardware.min_vram_gib_per_gpu > spec["hardware"]["vram_gib_per_gpu"]
        ):
            raise AppError(422, "Serving profile does not match worker hardware")
        if profile.hardware.vendor != spec["hardware"]["vendor"] or (
            profile.hardware.gpu_model
            and profile.hardware.gpu_model != spec["hardware"]["gpu_model"]
        ):
            raise AppError(422, "Serving GPU model differs from worker hardware")
        if profile.topology.gpus_per_replica > spec["hardware"]["gpu_count"]:
            raise AppError(422, "A serving replica must fit within one worker instance")
        previous = db.get(Resource, body.id, with_for_update=True)
        if previous and (
            previous.kind != "deployment"
            or previous.active_operation_id
            or previous.payload["status"] not in {"failed", "unavailable"}
        ):
            raise AppError(409, "Deployment is already active")
        used = 0
        for existing in db.scalars(select(Resource).where(Resource.kind == "deployment")):
            if existing.id == body.id:
                continue
            value = existing.payload
            if value["group_id"] == body.group_id and value["status"] not in {
                "unavailable",
                "failed",
            }:
                topology = value["profile"]["topology"]
                used += topology["replicas"] * topology["gpus_per_replica"]
        if (
            used + profile.topology.replicas * profile.topology.gpus_per_replica
            > spec["desired_instances"] * spec["hardware"]["gpu_count"]
        ):
            raise AppError(422, "Deployment would exceed group GPU capacity")
        payload = DeploymentView(
            id=body.id, profile=profile, group_id=body.group_id, status="deploying"
        ).model_dump(mode="json")
        if previous:
            previous.payload = payload
            previous.active_operation_id = op_id
        else:
            db.add(
                Resource(id=body.id, kind="deployment", active_operation_id=op_id, payload=payload)
            )

    return accepted_headers(
        response, submit(db, key, "deploy_serving", body.id, body.model_dump(mode="json"), mutate)
    )


@router.post("/runs/preflight", response_model=Preflight, operation_id="preflightRun")
def preflight(body: RunLaunch, db: Db):
    return assess(db, body, snapshot(db, body))


@router.get("/runs", response_model=list[RunView], operation_id="listRuns")
def runs(db: Db):
    return [
        RunView(
            run=r.payload,
            timeline_origin_at=r.context.get("timeline_origin_at"),
            mode=r.context["mode"],
            group_ids=r.context.get("group_ids", {}),
            source=r.context.get("source", "management"),
            provenance=r.context.get(
                "provenance", "synthetic" if r.payload.get("fixture") else "unverified"
            ),
        )
        for r in db.scalars(select(RunRow).order_by(RunRow.id))
    ]


@router.get("/runs/{identity}", response_model=RunView, operation_id="getRun")
def get_run(identity: str, db: Db):
    row = db.get(RunRow, identity)
    if not row:
        raise AppError(404, "Run not found")
    return RunView(
        run=row.payload,
        timeline_origin_at=row.context.get("timeline_origin_at"),
        mode=row.context["mode"],
        group_ids=row.context.get("group_ids", {}),
        source=row.context.get("source", "management"),
        provenance=row.context.get(
            "provenance", "synthetic" if row.payload.get("fixture") else "unverified"
        ),
    )


@router.post("/runs", response_model=Accepted, status_code=202, operation_id="startRun")
def start_run(body: RunLaunch, db: Db, response: Response, key: Idempotency):
    identity = "run-" + digest(key)[:16]

    def mutate(op_id):
        snap = snapshot(db, body)
        if body.mode == "real" and (snap.target is None or snap.target.mode != "benchmark"):
            raise AppError(422, "Real launches require a benchmark execution target")
        if body.mode == "simulation" and snap.target and snap.target.mode != "local-stub":
            raise AppError(422, "Simulation cannot execute a live benchmark target")
        report = assess(db, body, snap)
        if body.mode == "real" and not report.executable:
            raise AppError(
                422,
                "Real execution blocked: "
                + "; ".join(report.contract_blockers + report.runtime_blockers),
                "preflight_failed",
            )
        if body.mode == "simulation" and not report.simulation_available:
            raise AppError(
                422,
                "Simulation needs ready simulated resources: " + "; ".join(report.runtime_blockers),
                "runtime_not_ready",
            )
        run = Run(
            id=identity,
            fixture=body.mode == "simulation",
            status="queued",
            created_at=now(),
            snapshot=snap,
        )
        db.add(
            RunRow(
                id=identity,
                snapshot=snap.model_dump(mode="json"),
                payload=run.model_dump(mode="json"),
                context={
                    "mode": body.mode,
                    "outcome": body.outcome,
                    "group_ids": body.group_ids,
                    "source": "management",
                    "provenance": "synthetic" if body.mode == "simulation" else "measured",
                    "prepared_run_id": body.prepared_run_id,
                },
            )
        )

    return accepted_headers(
        response, submit(db, key, "start_run", identity, body.model_dump(mode="json"), mutate)
    )


@router.post(
    "/runs/{identity}/stop", response_model=Accepted, status_code=202, operation_id="stopRun"
)
def stop_run(identity: str, db: Db, response: Response, key: Idempotency):
    def mutate(op_id):
        row = db.get(RunRow, identity, with_for_update=True)
        if not row:
            raise AppError(404, "Run not found")
        if row.payload["status"] in {"completed", "failed", "cancelled"}:
            raise AppError(409, "Run is already terminal")
        row.context = {**row.context, "stop_requested": True}

    return accepted_headers(response, submit(db, key, "stop_run", identity, {}, mutate))


@router.get("/operations", response_model=list[OperationView], operation_id="listOperations")
def operations(db: Db):
    return [operation_view(db, row) for row in db.scalars(select(Operation).order_by(Operation.id))]


def operation_view(db, row):
    job = db.get(Job, row.id)
    events = db.scalars(select(Event).where(Event.operation_id == row.id).order_by(Event.id))
    return OperationView(
        operation=row.payload,
        attempt=job.attempt if job else 0,
        events=[{"created_at": e.created_at, "message": e.message} for e in events],
    )


@router.get("/operations/{identity}", response_model=OperationView, operation_id="getOperation")
def operation(identity: str, db: Db):
    row = db.get(Operation, identity)
    if not row:
        raise AppError(404, "Operation not found")
    return operation_view(db, row)


@router.get("/capacity", response_model=Capacity, operation_id="getCapacity")
def capacity(db: Db):
    row = db.get(Observation, "lambda")
    value = Capacity.model_validate(row.payload) if row else Capacity()
    if value.observed_at and (now() - value.observed_at).total_seconds() > 90:
        value.status = "stale"
    return value


@router.post("/capacity/refresh", response_model=Capacity, operation_id="refreshCapacity")
def refresh(db: Db):
    refresh_capacity()  # Coordinated and rate-limited; fetches only when due.
    return capacity(db)
