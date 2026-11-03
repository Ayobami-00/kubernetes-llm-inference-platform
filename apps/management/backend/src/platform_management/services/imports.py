"""Validated immutable imports. Artifact I/O happens before the application transaction."""

import copy
import threading
from datetime import timedelta

from platform_contracts import DatasetManifest, InfrastructureInventory, Run
from sqlalchemy import select, text, update

from platform_management.api.import_schemas import (
    DatasetImport,
    ExternalCapacityImport,
    FoundationObservation,
    InventoryImport,
    PreparedImport,
    RunImport,
)
from platform_management.db.models import (
    ArtifactCheck,
    ImportReceipt,
    InventorySnapshot,
    Job,
    Observation,
    PreparedRun,
    Revision,
    RunRow,
    Spec,
    Submission,
)
from platform_management.db.session import engine, session
from platform_management.integrations.artifacts import LocalArtifactAccess
from platform_management.jobs.queue import owned
from platform_management.services.common import AppError, digest, identifier, lock_key, now
from platform_management.settings import get_settings

MODELS = {
    "inventory": InventoryImport,
    "datasets": DatasetImport,
    "prepared-runs": PreparedImport,
    "runs": RunImport,
    "external-capacity": ExternalCapacityImport,
}


def validate(kind, payload):
    model = MODELS[kind].model_validate(payload)

    def reject_secrets(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in {
                    "password",
                    "private_key",
                    "api_key",
                    "bearer_token",
                    "join_token",
                    "kubeconfig",
                    "aws_access_key_id",
                    "aws_secret_access_key",
                    "aws_session_token",
                    "secret_access_key",
                    "authorization",
                }:
                    raise AppError(
                        422, "Imports accept credential references, never inline secrets"
                    )
                reject_secrets(child)
        elif isinstance(value, list):
            for child in value:
                reject_secrets(child)
        elif (
            isinstance(value, str)
            and value.startswith(("http://", "https://"))
            and not any(c.isspace() for c in value)
        ):
            from urllib.parse import parse_qs, urlsplit

            url = urlsplit(value)
            sensitive = {
                "token",
                "key",
                "api_key",
                "apikey",
                "authorization",
                "password",
                "signature",
            }
            if (
                url.username
                or url.password
                or sensitive & {key.lower() for key in parse_qs(url.query)}
            ):
                raise AppError(
                    422, "Import URLs must use credential references, never embedded values"
                )

    reject_secrets(model.model_dump(mode="json"))
    doc = model.document
    if kind == "runs" and doc.status not in {"completed", "failed", "cancelled"}:
        raise AppError(422, "Only terminal external runs can be imported", "invalid_import")
    if isinstance(doc, FoundationObservation):
        nodes = doc.nodes
        cluster = doc.allocation.get("cluster_id")
        if not cluster or len({n.id for n in nodes}) != len(nodes):
            raise AppError(422, "Foundation projection needs a cluster ID and unique nodes")
        if any(n.provider != "aws" or n.role != "control_plane" for n in nodes):
            raise AppError(422, "Foundation exports describe AWS control-plane observations")
        if any(n.observed_at > doc.captured_at for n in nodes):
            raise AppError(422, "Node observation is newer than foundation capture")
    if hasattr(doc, "fixture") and not doc.fixture and model.provenance == "synthetic":
        raise AppError(422, "Synthetic runtime records must explicitly set fixture=true")
    fixture = getattr(doc, "fixture", False) or model.provenance == "synthetic"
    if fixture and model.provenance == "measured":
        raise AppError(
            422, "Fixture records cannot claim measured provenance", "invalid_provenance"
        )
    if fixture:
        model = model.model_copy(update={"provenance": "synthetic"})
    return model


def submit_import(db, kind, body, key):
    data = validate(kind, body.model_dump(mode="json")).model_dump(mode="json")
    if len(str(data).encode()) > get_settings().import_max_bytes:
        raise AppError(413, "Import document exceeds the configured size limit", "import_too_large")
    fingerprint = digest({"kind": kind, **data})
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key("submission:" + key)})
    previous = db.scalar(select(ImportReceipt).where(ImportReceipt.submission_key == key))
    if previous:
        if previous.fingerprint != fingerprint:
            raise AppError(
                409, "Idempotency key was used for a different import", "idempotency_conflict"
            )
        return previous.id
    if db.get(Submission, key):
        raise AppError(409, "Idempotency key already belongs to a workflow", "idempotency_conflict")
    identity = identifier("import")
    db.add(
        ImportReceipt(
            id=identity,
            kind=kind,
            submission_key=key,
            fingerprint=fingerprint,
            document=data["document"],
            source=data["source"],
            provenance=data["provenance"],
            status="queued",
            result={},
        )
    )
    db.add(Job(id=identity, kind="import", checkpoint={}, due_at=now()))
    return identity


def artifact_references(document):
    """Find shared Artifact/PreparedDataset structures without interpreting benchmark data."""
    found = {}

    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("uri"), str):
                uri, sha = value["uri"], value.get("sha256")
                found[digest({"uri": uri, "sha256": sha})] = (uri, sha)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(document)
    return found


def inspect_artifacts(document, root=None):
    access = LocalArtifactAccess(root or get_settings().artifacts_root)
    checks = []
    for identity, (uri, sha) in artifact_references(document).items():
        error = None
        if not sha:
            status, error = "unverified", "No checksum supplied"
        else:
            status = "verified" if access.verify(uri, sha) else "blocked"
            if status == "blocked":
                error = "Artifact missing, outside the artifact root, or checksum differs"
        checks.append(
            {
                "id": identity,
                "uri": uri,
                "sha256": sha,
                "status": status,
                "error": error,
                "checked_at": now(),
            }
        )
    return checks


def conflict(existing, document, label):
    if existing is not None and digest(existing) != digest(document):
        raise AppError(
            409, f"{label} already exists with different immutable content", "immutable_conflict"
        )


def apply_import(db, row, checks):
    document, kind = row.document, row.kind
    for check in checks:
        db.merge(ArtifactCheck(**check))
    result = {"artifacts": [{"id": c["id"], "status": c["status"]} for c in checks]}
    if kind == "inventory":
        complete = document.get("schema_version") != "foundation.v1"
        if complete:
            inventory = InfrastructureInventory.model_validate(document)
            identity, cluster, captured = (
                inventory.snapshot_id,
                inventory.cluster.id,
                inventory.captured_at,
            )
            fixture = inventory.fixture
        else:
            observation = FoundationObservation.model_validate(document)
            identity = "foundation-" + digest(document)[:24]
            cluster, captured = observation.allocation["cluster_id"], observation.captured_at
            fixture = row.provenance == "synthetic"
        db.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": lock_key("inventory-import:" + identity)},
        )
        previous = db.get(InventorySnapshot, identity)
        conflict(previous.document if previous else None, document, "Inventory snapshot")
        if not previous:
            db.add(
                InventorySnapshot(
                    id=identity,
                    cluster_id=cluster,
                    captured_at=captured,
                    document=document,
                    digest=digest(document),
                    source=row.source,
                    complete=complete,
                    fixture=fixture,
                )
            )
        pointer_id = "inventory:" + ("full:" if complete else "foundation:") + cluster
        pointer = db.get(Observation, pointer_id, with_for_update=True)
        # Serialize concurrent imports per cluster as well as per identity.
        db.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": lock_key("inventory-current:" + pointer_id)},
        )
        pointer = db.get(Observation, pointer_id, populate_existing=True)
        if not pointer or captured.isoformat() > pointer.payload["captured_at"]:
            db.merge(
                Observation(
                    id=pointer_id,
                    payload={"snapshot_id": identity, "captured_at": captured.isoformat()},
                )
            )
        result["resource_id"] = identity
    elif kind == "external-capacity":
        from platform_management.services.external_capacity import apply_capacity

        result.update(apply_capacity(db, document))
    elif kind == "datasets":
        dataset = DatasetManifest.model_validate(document)
        key = "dataset-manifest:" + dataset.id
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key("spec:" + key)})
        revision_key = f"{key}@{dataset.revision}"
        old = db.get(Revision, revision_key)
        conflict(old.document if old else None, document, "Dataset revision")
        spec = db.get(Spec, key, with_for_update=True)
        if not spec:
            spec = Spec(
                key=key,
                kind="dataset-manifest",
                resource_id=dataset.id,
                current_revision=0,
                version=0,
            )
            db.add(spec)
            db.flush()
        if not old:
            db.add(
                Revision(
                    key=revision_key, spec_key=key, revision=dataset.revision, document=document
                )
            )
        if dataset.revision > spec.current_revision:
            if spec.draft:
                raise AppError(409, "Import would conflict with an existing working draft")
            spec.current_revision, spec.version = dataset.revision, spec.version + 1
        result["resource_id"] = dataset.ref
    elif kind == "prepared-runs":
        identity = document["id"]
        db.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key("prepared-import:" + identity)}
        )
        old = db.get(PreparedRun, identity)
        if old and (
            old.fixture != document["fixture"]
            or digest(old.snapshot) != digest(document["snapshot"])
        ):
            raise AppError(
                409, "Prepared bundle already exists with different content", "immutable_conflict"
            )
        if not old:
            db.add(
                PreparedRun(
                    id=identity,
                    digest=digest(document["snapshot"]),
                    snapshot=document["snapshot"],
                    fixture=document["fixture"],
                    source=row.source,
                )
            )
        result["resource_id"] = identity
    elif kind == "runs":
        run = Run.model_validate(document)
        db.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key("run-import:" + run.id)}
        )
        old = db.get(RunRow, run.id)
        conflict(old.payload if old else None, document, "Historical run")
        if not old:
            db.add(
                RunRow(
                    id=run.id,
                    snapshot=run.snapshot.model_dump(mode="json"),
                    payload=document,
                    context={
                        "mode": "simulation" if run.fixture else "real",
                        "source": row.source,
                        "provenance": row.provenance,
                        "origin": "import",
                        "import_id": row.id,
                        "group_ids": {},
                    },
                )
            )
        result["resource_id"] = run.id
    return result


def execute_import(identity, owner):
    key = lock_key("execution:" + identity)
    with engine().connect().execution_options(isolation_level="AUTOCOMMIT") as guard:
        if not guard.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}):
            with session() as db, db.begin():
                job = owned(db, identity, owner)
                job.state, job.owner, job.due_at = "pending", None, now() + timedelta(seconds=1)
            return
        stop = threading.Event()

        def heartbeat():
            while not stop.wait(get_settings().heartbeat_seconds):
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
                owned(db, identity, owner)
                row = db.get(ImportReceipt, identity, with_for_update=True)
                row.status = "running"
                document = copy.deepcopy(row.document)
            checks = inspect_artifacts(document)  # No database transaction during file I/O.
            with session() as db, db.begin():
                job = owned(db, identity, owner)
                row = db.get(ImportReceipt, identity, with_for_update=True)
                row.result = apply_import(db, row, checks)
                row.status, row.finished_at, row.error = "completed", now(), None
                job.state, job.owner = "completed", None
        except Exception as exc:
            with session() as db, db.begin():
                job = owned(db, identity, owner)
                row = db.get(ImportReceipt, identity, with_for_update=True)
                job.attempt += 1
                permanent = isinstance(exc, (ValueError, AppError))
                terminal = permanent or job.attempt >= 5
                # No SQL/provider response bodies or credentials in public error details.
                row.error = str(exc)[:500] if permanent else "Import persistence failed"
                row.status = "failed" if terminal else "queued"
                row.finished_at = now() if terminal else None
                job.state, job.owner = ("failed" if terminal else "pending"), None
                job.due_at = now() + timedelta(seconds=min(30, 2**job.attempt))
        finally:
            stop.set()
            thread.join(timeout=2)
            guard.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})


def receipt_view(row):
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "kind",
            "source",
            "provenance",
            "status",
            "created_at",
            "finished_at",
            "error",
            "result",
        )
    }


def current_inventory_ids(db):
    return {
        row.payload["snapshot_id"]
        for row in db.scalars(select(Observation).where(Observation.id.like("inventory:%")))
    }


def inventory_view(row, current_ids):
    return {
        **{
            key: getattr(row, key)
            for key in (
                "id",
                "cluster_id",
                "source",
                "complete",
                "fixture",
                "captured_at",
                "document",
            )
        },
        "current": row.id in current_ids,
    }
