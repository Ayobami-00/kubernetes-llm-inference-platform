import yaml
from fastapi import APIRouter, Depends, Response
from platform_contracts import ServingBinding, validate_catalog
from platform_contracts.loading import CONFIG_FOLDERS
from sqlalchemy import select

from platform_management.api.dependencies import Db, administrator
from platform_management.api.schemas import Publish, SpecView, SpecWrite
from platform_management.db.models import Resource, Revision, Spec
from platform_management.services import catalog as service
from platform_management.services.common import AppError

router = APIRouter(dependencies=[Depends(administrator)], tags=["Specifications"])


def register(kind, path, name):
    @router.get("/" + path, response_model=list[SpecView], operation_id=f"list{name}")
    def listing(db: Db):
        return [
            service.view(db, row)
            for row in db.scalars(select(Spec).where(Spec.kind == kind).order_by(Spec.resource_id))
        ]

    @router.put(
        "/" + path + "/{resource_id}/draft",
        response_model=SpecView,
        operation_id=f"save{name}Draft",
    )
    def write(resource_id: str, body: SpecWrite, db: Db):
        if kind == "dataset-manifest":
            raise AppError(405, "Dataset manifests are supplied by Stream A")
        return service.write_draft(
            db, kind, resource_id, body.document.model_dump(mode="json"), body.expected_version
        )

    @router.post(
        "/" + path + "/{resource_id}/publish",
        response_model=SpecView,
        operation_id=f"publish{name}",
    )
    def publish(resource_id: str, body: Publish, db: Db):
        return service.publish(db, kind, resource_id, body.expected_version)

    @router.get(
        "/" + path + "/{resource_id}/revisions",
        response_model=list[SpecView],
        operation_id=f"history{name}",
    )
    def history(resource_id: str, db: Db):
        row = db.get(Spec, f"{kind}:{resource_id}")
        if not row:
            raise AppError(404, "Configuration not found")
        return [
            SpecView(
                id=row.resource_id,
                kind=kind,
                current_revision=revision.revision,
                version=row.version,
                document=revision.document,
                draft=None,
            )
            for revision in db.scalars(
                select(Revision)
                .where(Revision.spec_key == row.key)
                .order_by(Revision.revision.desc())
            )
        ]

    @router.get("/" + path + "/{resource_id}/export", operation_id=f"export{name}")
    def export(resource_id: str, db: Db):
        row = db.get(Spec, f"{kind}:{resource_id}")
        if not row or not row.current_revision:
            raise AppError(404, "Published configuration not found")
        doc = db.get(Revision, f"{row.key}@{row.current_revision}").document
        return Response(
            yaml.safe_dump(doc, sort_keys=False),
            media_type="application/yaml",
            headers={"Content-Disposition": f'attachment; filename="{resource_id}.yaml"'},
        )


for kind, path, name in [
    ("customer", "customers", "Customers"),
    ("scenario", "scenarios", "Scenarios"),
    ("serving-profile", "serving-profiles", "ServingProfiles"),
    ("worker-group", "worker-group-specs", "WorkerGroupSpecs"),
    ("dataset-manifest", "datasets", "Datasets"),
]:
    register(kind, path, name)


@router.get("/assignments", response_model=list[ServingBinding], operation_id="listAssignments")
def assignments(db: Db):
    return list(db.scalars(select(Resource.payload).where(Resource.kind == "assignment")))


@router.put(
    "/assignments/{customer_id}", response_model=ServingBinding, operation_id="saveAssignment"
)
def save_assignment(customer_id: str, body: ServingBinding, db: Db):
    if body.customer_ref.split("@")[0] != customer_id:
        raise AppError(422, "Assignment customer differs from URL")
    cat = service.catalog(db)
    customer = cat.get("customer", body.customer_ref)
    profile = cat.get("serving-profile", body.serving_profile_ref)
    cat.get("worker-group", body.worker_group_ref)
    if customer.model_alias != profile.model.alias:
        raise AppError(422, "Customer model alias differs from serving profile")
    # Validate every complete affected scenario, including shared deployment GPU accounting.
    db.merge(
        Resource(
            id=f"assignment-{customer_id}", kind="assignment", payload=body.model_dump(mode="json")
        )
    )
    db.flush()
    from platform_contracts import build_run_snapshot

    for scenario in cat.records["scenario"].values():
        if body.customer_ref in scenario.participants:
            build_run_snapshot(cat, scenario.ref, service.bindings(db, scenario))
    validate_catalog(cat)
    return body


@router.get("/configuration/export", operation_id="exportConfiguration")
def export_all(db: Db):
    import io
    import zipfile

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for row in db.scalars(select(Spec).where(Spec.current_revision > 0)):
            revision = db.get(Revision, f"{row.key}@{row.current_revision}")
            archive.writestr(
                f"{CONFIG_FOLDERS[row.kind]}/{row.resource_id}.yaml",
                yaml.safe_dump(revision.document, sort_keys=False),
            )
    return Response(
        stream.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="configuration.zip"'},
    )
