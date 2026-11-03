from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import FileResponse
from sqlalchemy import select

from platform_management.api.dependencies import Db, administrator
from platform_management.api.import_schemas import (
    ArtifactView,
    DatasetImport,
    ImportAccepted,
    ImportView,
    IntegrationReport,
    InventoryImport,
    InventoryView,
    PreparedImport,
    PreparedView,
    RunImport,
)
from platform_management.db.models import (
    ArtifactCheck,
    ImportReceipt,
    InventorySnapshot,
    Observation,
    PreparedRun,
)
from platform_management.db.session import session
from platform_management.integrations.artifacts import LocalArtifactAccess
from platform_management.integrations.registry import capabilities
from platform_management.services.common import AppError
from platform_management.services.imports import (
    current_inventory_ids,
    inventory_view,
    receipt_view,
    submit_import,
)
from platform_management.settings import get_settings

router = APIRouter(dependencies=[Depends(administrator)], tags=["Integrations"])
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)]


def accepted(db, response, key, kind, body):
    identity = submit_import(db, kind, body, key)
    response.headers["Location"] = "/api/v1/imports/" + identity
    return ImportAccepted(import_id=identity)


@router.post(
    "/imports/inventory",
    response_model=ImportAccepted,
    status_code=202,
    operation_id="importInventory",
)
def import_inventory(body: InventoryImport, db: Db, response: Response, key: Key):
    return accepted(db, response, key, "inventory", body)


@router.post(
    "/imports/datasets",
    response_model=ImportAccepted,
    status_code=202,
    operation_id="importDataset",
)
def import_dataset(body: DatasetImport, db: Db, response: Response, key: Key):
    return accepted(db, response, key, "datasets", body)


@router.post(
    "/imports/prepared-runs",
    response_model=ImportAccepted,
    status_code=202,
    operation_id="importPreparedRun",
)
def import_prepared(body: PreparedImport, db: Db, response: Response, key: Key):
    return accepted(db, response, key, "prepared-runs", body)


@router.post(
    "/imports/runs",
    response_model=ImportAccepted,
    status_code=202,
    operation_id="importHistoricalRun",
)
def import_run(body: RunImport, db: Db, response: Response, key: Key):
    return accepted(db, response, key, "runs", body)


@router.get("/imports", response_model=list[ImportView], operation_id="listImports")
def list_imports(db: Db):
    return [
        receipt_view(r)
        for r in db.scalars(select(ImportReceipt).order_by(ImportReceipt.created_at.desc()))
    ]


@router.get("/imports/{identity}", response_model=ImportView, operation_id="getImport")
def get_import(identity: str, db: Db):
    row = db.get(ImportReceipt, identity)
    if not row:
        raise AppError(404, "Import not found")
    return receipt_view(row)


@router.get("/inventory", response_model=list[InventoryView], operation_id="listInventory")
def list_inventory(db: Db):
    ids = current_inventory_ids(db)
    return [
        inventory_view(r, ids)
        for r in db.scalars(
            select(InventorySnapshot).order_by(InventorySnapshot.captured_at.desc())
        )
    ]


@router.get("/inventory/{identity}", response_model=InventoryView, operation_id="getInventory")
def get_inventory(identity: str, db: Db):
    row = db.get(InventorySnapshot, identity)
    if not row:
        raise AppError(404, "Inventory snapshot not found")
    return inventory_view(row, current_inventory_ids(db))


@router.get("/prepared-runs", response_model=list[PreparedView], operation_id="listPreparedRuns")
def prepared_runs(db: Db):
    return [
        {k: getattr(row, k) for k in ("id", "source", "fixture", "digest", "snapshot")}
        for row in db.scalars(select(PreparedRun).order_by(PreparedRun.created_at.desc()))
    ]


@router.get("/integrations", response_model=IntegrationReport, operation_id="getIntegrations")
def integrations(db: Db):
    s = get_settings()
    from platform_management.api.schemas import ReadinessCheck

    inventory = list(db.scalars(select(InventorySnapshot)))
    real_full = [r for r in inventory if r.complete and not r.fixture]
    current = [r for r in real_full if r.id in current_inventory_ids(db)]
    from platform_management.services.common import now

    fresh = any(
        0 <= (now() - r.captured_at).total_seconds() <= s.cluster_observation_max_age_seconds
        for r in current
    )
    cluster = db.get(Observation, "cluster-readiness")
    cluster_observed = cluster.payload.get("observed_at") if cluster else None
    cluster_fresh = bool(
        cluster_observed
        and 0
        <= (now() - datetime.fromisoformat(cluster_observed)).total_seconds()
        <= s.cluster_observation_max_age_seconds
        and cluster.payload.get("status") == "fresh"
    )
    checks = [
        ReadinessCheck(
            code="inventory",
            label="Full infrastructure inventory",
            status="ready" if fresh else "stale" if current else "unconfigured",
            detail="Import current, complete non-fixture inventory from B",
        ),
        ReadinessCheck(
            code="artifact-storage",
            label="Shared artifact filesystem",
            status="ready" if s.artifact_claim else "unconfigured",
            detail="Configure a shared artifact PVC mounted consistently at the artifact root",
        ),
        ReadinessCheck(
            code="cluster",
            label="Read-only Kubernetes observations",
            status="ready" if cluster_fresh else "stale" if cluster_observed else "unconfigured",
            detail="Cluster observations do not establish host or WireGuard readiness",
        ),
    ]
    return IntegrationReport(
        checks=checks,
        cluster_observation=cluster.payload if cluster else None,
        adapters=capabilities(),
        artifacts_root=str(s.artifacts_root),
        inventory_max_age_seconds=s.cluster_observation_max_age_seconds,
    )


@router.get("/artifacts", response_model=list[ArtifactView], operation_id="listArtifacts")
def artifacts(db: Db):
    return [
        {k: getattr(row, k) for k in ("id", "uri", "sha256", "status", "error", "checked_at")}
        for row in db.scalars(select(ArtifactCheck).order_by(ArtifactCheck.checked_at.desc()))
    ]


@router.get("/artifacts/{identity}", response_class=FileResponse, operation_id="downloadArtifact")
def download(identity: str):
    # End the read transaction before hashing or streaming a potentially large artifact.
    with session() as db:
        row = db.get(ArtifactCheck, identity)
        if not row:
            raise AppError(404, "Registered artifact not found")
        uri, sha, status = row.uri, row.sha256, row.status
    access = LocalArtifactAccess(get_settings().artifacts_root)
    if status != "verified" or not sha or not access.verify(uri, sha):
        raise AppError(
            409, "Artifact is unavailable or no longer matches its checksum", "artifact_unverified"
        )
    path = access.path(uri)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=path.name,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
