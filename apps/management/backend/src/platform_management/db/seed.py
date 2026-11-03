from pathlib import Path

from platform_contracts import Run, ServingBinding, load_catalog
from sqlalchemy import select, text

from platform_management.db.models import Resource, Revision, RunRow, Spec
from platform_management.db.session import session
from platform_management.settings import get_settings

ASSIGNMENTS = {
    "writer-assist": ("gec-baseline", "latency-workers", "gec"),
    "code-agent": ("code-agent-baseline", "large-workers", "code-large"),
    "tutor-chat": ("shared-small-baseline", "shared-workers", "shared-small"),
    "doc-flow": ("shared-medium-baseline", "shared-workers", "shared-medium"),
    "research-agent": ("shared-medium-baseline", "shared-workers", "shared-medium"),
}


def seed(db, root: Path):
    db.execute(text("SELECT pg_advisory_xact_lock(73490123)"))
    cat = load_catalog(root)
    for kind, records in cat.records.items():
        for doc in records.values():
            key = f"{kind}:{doc.id}"
            if db.get(Spec, key) is None:
                db.add(
                    Spec(
                        key=key,
                        kind=kind,
                        resource_id=doc.id,
                        current_revision=doc.revision,
                        version=1,
                    )
                )
                db.flush()
                db.add(
                    Revision(
                        key=f"{key}@{doc.revision}",
                        spec_key=key,
                        revision=doc.revision,
                        document=doc.model_dump(mode="json"),
                    )
                )
    for customer, (profile, group, deployment) in ASSIGNMENTS.items():
        identity = f"assignment-{customer}"
        if db.get(Resource, identity) is None:
            binding = ServingBinding(
                customer_ref=f"{customer}@1",
                serving_profile_ref=f"{profile}@1",
                worker_group_ref=f"{group}@1",
                deployment_id=deployment,
            )
            db.add(
                Resource(id=identity, kind="assignment", payload=binding.model_dump(mode="json"))
            )
    for path in sorted((root / "contracts/fixtures/runs").glob("*/*.json")):
        run = Run.model_validate_json(path.read_text())
        # Only historical terminal examples: queued fixtures must never look like active jobs.
        if run.status in {"queued", "running"} or db.get(RunRow, run.id):
            continue
        db.add(
            RunRow(
                id=run.id,
                snapshot=run.snapshot.model_dump(mode="json"),
                payload=run.model_dump(mode="json"),
                context={"mode": "simulation", "seed": True},
            )
        )
    db.flush()
    return len(list(db.scalars(select(Spec))))


def main():
    with session() as db, db.begin():
        count = seed(db, get_settings().repository_root)
    print(f"Seed complete: {count} specifications; existing user data preserved.")


if __name__ == "__main__":
    main()
