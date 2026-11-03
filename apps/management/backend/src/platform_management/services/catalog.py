from platform_contracts import ContractCatalog, build_run_snapshot, validate_catalog
from platform_contracts.loading import CONFIG_MODELS
from sqlalchemy import select

from platform_management.api.schemas import SpecView
from platform_management.db.models import PreparedRun, Resource, Revision, Spec
from platform_management.services.common import AppError


def catalog(db):
    result = ContractCatalog()
    for row in db.scalars(select(Revision).order_by(Revision.key)):
        kind = row.spec_key.split(":", 1)[0]
        result.add(kind, CONFIG_MODELS[kind].model_validate(row.document))
    return result


def view(db, row):
    rev = db.get(Revision, f"{row.key}@{row.current_revision}")
    return SpecView(
        id=row.resource_id,
        kind=row.kind,
        current_revision=row.current_revision,
        version=row.version,
        document=rev.document if rev else None,
        draft=row.draft,
    )


def write_draft(db, kind, resource_id, document, expected_version):
    doc = CONFIG_MODELS[kind].model_validate(document)
    if doc.id != resource_id:
        raise AppError(422, "The document ID must match the resource ID")
    row = db.get(Spec, f"{kind}:{resource_id}", with_for_update=True)
    if row is None:
        if expected_version != 0:
            raise AppError(409, "The configuration does not exist", "version_conflict")
        row = Spec(
            key=f"{kind}:{resource_id}",
            kind=kind,
            resource_id=resource_id,
            current_revision=0,
            version=0,
        )
        db.add(row)
    if row.version != expected_version:
        raise AppError(409, "This draft changed. Reload before saving.", "version_conflict")
    row.draft = doc.model_copy(update={"revision": row.current_revision + 1}).model_dump(
        mode="json"
    )
    row.version += 1
    db.flush()
    return view(db, row)


def publish(db, kind, resource_id, expected_version):
    row = db.get(Spec, f"{kind}:{resource_id}", with_for_update=True)
    if not row:
        raise AppError(404, "Configuration not found")
    if row.version != expected_version:
        raise AppError(409, "This draft changed. Reload before publishing.", "version_conflict")
    if not row.draft:
        raise AppError(409, "Save a draft before publishing")
    doc = CONFIG_MODELS[kind].model_validate(row.draft)
    cat = catalog(db)
    cat.add(kind, doc)
    validate_catalog(cat)
    row.current_revision = doc.revision
    db.add(
        Revision(
            key=f"{row.key}@{doc.revision}",
            spec_key=row.key,
            revision=doc.revision,
            document=doc.model_dump(mode="json"),
        )
    )
    row.draft = None
    row.version += 1
    db.flush()
    return view(db, row)


def bindings(db, scenario):
    from platform_contracts import ServingBinding

    result = []
    for ref in scenario.participants:
        row = db.get(Resource, f"assignment-{ref.split('@')[0]}")
        if not row:
            raise AppError(422, f"Missing serving assignment for {ref}")
        result.append(ServingBinding.model_validate(row.payload))
    return tuple(result)


def snapshot(db, launch):
    if launch.prepared_run_id:
        row = db.get(PreparedRun, launch.prepared_run_id)
        if not row:
            raise AppError(404, "Prepared run not found")
        from platform_contracts import RunSnapshot

        prepared = RunSnapshot.model_validate(row.snapshot)
        if prepared.scenario.ref != launch.scenario_ref:
            raise AppError(422, "Prepared snapshot does not match the selected scenario")
        if launch.bindings and [b.model_dump() for b in launch.bindings] != [
            b.model_dump() for b in prepared.bindings
        ]:
            raise AppError(422, "Serving bindings differ from the frozen prepared snapshot")
        return prepared
    cat = catalog(db)
    scenario = cat.get("scenario", launch.scenario_ref)
    return build_run_snapshot(
        cat,
        launch.scenario_ref,
        tuple(launch.bindings) if launch.bindings else bindings(db, scenario),
    )
