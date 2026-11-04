"""Authenticated fixed-preset experiment workflows and immutable history."""

import copy

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select

from platform_management.api.dependencies import Db, administrator
from platform_management.api.experiment_schemas import (
    ExperimentLaunch,
    ExperimentPreview,
    ExperimentView,
)
from platform_management.api.routers.actions import Idempotency, accepted_headers
from platform_management.api.schemas import Accepted
from platform_management.db.models import Resource
from platform_management.jobs.queue import submit
from platform_management.services.a10_experiments import definition, reserve
from platform_management.services.common import AppError, digest, now

router = APIRouter(dependencies=[Depends(administrator)], tags=["Experiments"])


@router.get(
    "/experiments/a10/preview",
    response_model=ExperimentPreview,
    operation_id="previewA10Experiment",
)
def preview(db: Db):
    try:
        frozen = definition(db)
        return ExperimentPreview(
            available=True, definition_sha256=digest(frozen), summary=frozen["summary"]
        )
    except (AppError, ValueError, OSError) as exc:
        return ExperimentPreview(
            available=False, blockers=[exc.message if isinstance(exc, AppError) else str(exc)]
        )


@router.post(
    "/experiments/a10", response_model=Accepted, status_code=202, operation_id="startA10Experiment"
)
def launch(body: ExperimentLaunch, db: Db, response: Response, key: Idempotency):
    identity = "experiment-" + digest(key)[:20]

    def mutate(operation_id):
        frozen = definition(db)
        if digest(frozen) != body.definition_sha256:
            raise AppError(
                409, "Experiment inputs changed; review the latest preview", "version_conflict"
            )
        reserve(db, operation_id)
        payload = ExperimentView(
            id=identity,
            status="queued",
            stage="preflight",
            operation_id=operation_id,
            created_at=now(),
            definition_sha256=body.definition_sha256,
            summary=frozen["summary"],
        ).model_dump(mode="json")
        db.add(
            Resource(
                id=identity,
                kind="experiment",
                payload={**payload, "definition": frozen},
                active_operation_id=operation_id,
            )
        )

    return accepted_headers(
        response,
        submit(db, key, "start_experiment", identity, body.model_dump(mode="json"), mutate),
    )


@router.get("/experiments", response_model=list[ExperimentView], operation_id="listExperiments")
def experiments(db: Db):
    return [
        public(row.payload)
        for row in db.scalars(
            select(Resource).where(Resource.kind == "experiment").order_by(Resource.id.desc())
        )
    ]


def public(payload):
    return {k: v for k, v in payload.items() if k != "definition"}


@router.get("/experiments/{identity}", response_model=ExperimentView, operation_id="getExperiment")
def get_experiment(identity: str, db: Db):
    row = db.get(Resource, identity)
    if not row or row.kind != "experiment":
        raise AppError(404, "Experiment not found")
    return public(row.payload)


@router.post(
    "/experiments/{identity}/stop",
    response_model=Accepted,
    status_code=202,
    operation_id="stopExperiment",
)
def stop(identity: str, db: Db, response: Response, key: Idempotency):
    def mutate(operation_id):
        row = db.get(Resource, identity, with_for_update=True)
        if not row or row.kind != "experiment":
            raise AppError(404, "Experiment not found")
        if row.payload["status"] in {"completed", "failed", "cancelled"}:
            raise AppError(409, "Experiment is already terminal")
        row.payload = {**copy.deepcopy(row.payload), "stop_requested": True}

    return accepted_headers(response, submit(db, key, "stop_experiment", identity, {}, mutate))
