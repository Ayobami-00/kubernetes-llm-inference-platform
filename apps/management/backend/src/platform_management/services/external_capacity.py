"""Read-only B-owned resource onboarding. No provider or host mutation occurs here."""

from platform_contracts import InfrastructureInventory
from sqlalchemy import text

from platform_management.api.import_schemas import ExternalCapacity
from platform_management.api.schemas import DeploymentView, GroupView
from platform_management.db.models import InventorySnapshot, Observation, Resource, Revision, Spec
from platform_management.services.common import AppError, digest, lock_key


def apply_capacity(db, document):
    bundle = ExternalCapacity.model_validate(document)
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key("external:" + bundle.id)})
    old = db.get(Observation, "external-handoff:" + bundle.id)
    if old and old.payload != document:
        raise AppError(409, "External handoff identity already has different content")
    result = {
        "resource_id": bundle.id,
        "inventory_id": bundle.inventory_id,
        "resource_ids": [x.id for x in (*bundle.worker_groups, *bundle.deployments)],
        "digest": digest(document),
    }
    if old:
        return result
    row = db.get(InventorySnapshot, bundle.inventory_id)
    if not row or not row.complete or row.fixture:
        raise AppError(422, "Import a complete measured inventory before external capacity")
    inventory = InfrastructureInventory.model_validate(row.document)
    db.execute(
        text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": lock_key("capacity-cluster:" + inventory.cluster.id)},
    )
    nodes = {n.provider_instance_id: n for n in inventory.nodes if n.role == "gpu_worker"}
    resources = []
    for item in bundle.worker_groups:
        group = item.group
        record_revision(db, "worker-group", group.spec)
        if group.status.observed_at > inventory.captured_at:
            raise AppError(422, "Group observation is newer than its inventory")
        for member in group.status.members:
            node = nodes.get(member.provider_instance_id)
            if (
                not node
                or node.managed_by != "external"
                or node.worker_group_ref != group.spec.ref
                or node.hardware != group.spec.hardware
                or not node.kubernetes
                or node.kubernetes.uid != member.node_uid
                or node.kubernetes.name != member.node_name
            ):
                raise AppError(422, "External group members disagree with inventory identities")
        resources.append(
            (
                item.id,
                "worker_group",
                GroupView(
                    id=item.id,
                    mode="real",
                    ownership="external",
                    inventory_id=inventory.snapshot_id,
                    group=group,
                ).model_dump(mode="json"),
            )
        )
    for item in bundle.deployments:
        record_revision(db, "serving-profile", item.profile)
        resources.append(
            (
                item.id,
                "deployment",
                DeploymentView(
                    id=item.id,
                    profile=item.profile,
                    group_id=item.group_id,
                    status="ready",
                    mode="real",
                    ownership="external",
                    kubernetes_uid=item.kubernetes_uid,
                    endpoint=str(item.endpoint),
                ).model_dump(mode="json"),
            )
        )
    for identity, kind, payload in sorted(resources):
        db.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key("resource:" + identity)}
        )
        current = db.get(Resource, identity, with_for_update=True)
        if current:
            if (
                current.kind != kind
                or current.payload.get("ownership") != "external"
                or current.active_operation_id
            ):
                raise AppError(409, "External handoff cannot replace managed resources")
            prior_inventory = current.payload.get("inventory_id")
            if prior_inventory:
                previous = db.get(InventorySnapshot, prior_inventory)
                if previous and previous.captured_at > inventory.captured_at:
                    raise AppError(409, "External handoff cannot roll back inventory observations")
            if (
                kind == "worker_group"
                and current.payload["group"]["spec"] != payload["group"]["spec"]
            ):
                raise AppError(
                    409, "Use a new runtime identity for a different worker specification"
                )
            current.payload = payload
        else:
            db.add(Resource(id=identity, kind=kind, payload=payload))
    db.merge(Observation(id="external-handoff:" + bundle.id, payload=document))
    return result


def record_revision(db, kind, spec):
    key = kind + ":" + spec.id
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key("spec:" + key)})
    revision_key = f"{key}@{spec.revision}"
    previous = db.get(Revision, revision_key)
    if previous and previous.document != spec.model_dump(mode="json"):
        raise AppError(409, "External handoff conflicts with an immutable specification revision")
    row = db.get(Spec, key, with_for_update=True)
    if row is None:
        row = Spec(key=key, kind=kind, resource_id=spec.id, current_revision=0, version=0)
        db.add(row)
        db.flush()
    if not previous:
        db.add(
            Revision(
                key=revision_key,
                spec_key=key,
                revision=spec.revision,
                document=spec.model_dump(mode="json"),
            )
        )
        db.flush()
    if spec.revision > row.current_revision:
        if row.draft:
            raise AppError(409, "External revision conflicts with an existing working draft")
        row.current_revision, row.version = spec.revision, row.version + 1
