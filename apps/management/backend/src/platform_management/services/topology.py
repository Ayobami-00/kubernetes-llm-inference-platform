from platform_contracts import Run, RunSnapshot
from sqlalchemy import select

from platform_management.api.schemas import GraphEdge, GraphNode, Topology
from platform_management.db.models import InventorySnapshot, Observation, Resource, RunRow, Spec
from platform_management.services.catalog import catalog, view
from platform_management.services.common import AppError, now


def topology(db, run_id=None, include_history=False):
    nodes, edges = {}, {}
    historical = run_id is not None
    cat = catalog(db)
    runtime = {r.id: r for r in db.scalars(select(Resource))}
    if historical:
        selected = db.get(RunRow, run_id)
        if not selected:
            raise AppError(404, "Run not found")
        snap = RunSnapshot.model_validate(selected.snapshot)
        specs = [("customer", c) for c in snap.customers] + [
            ("dataset-manifest", d) for d in snap.datasets
        ]
        specs += [("scenario", snap.scenario)] + [("worker-group", g) for g in snap.worker_groups]
        profiles = {p.ref: p for p in snap.serving_profiles}
        bindings = list(snap.bindings)
    else:
        specs = []
        for row in db.scalars(select(Spec).where(Spec.current_revision > 0)):
            specs.append((row.kind, cat.get(row.kind, f"{row.resource_id}@{row.current_revision}")))
        from platform_contracts import ServingBinding

        bindings = [
            ServingBinding.model_validate(r.payload)
            for r in runtime.values()
            if r.kind == "assignment"
        ]
        profiles = cat.records["serving-profile"]
        for binding in bindings:
            specs.extend(
                [
                    ("customer", cat.get("customer", binding.customer_ref)),
                    ("worker-group", cat.get("worker-group", binding.worker_group_ref)),
                ]
            )

    def add(identity, kind, label, subtitle, status, provenance, resource_id, document):
        nodes[identity] = GraphNode(
            id=identity,
            kind=kind,
            label=label,
            subtitle=subtitle,
            status=status,
            provenance="historical" if historical else provenance,
            resource_id=resource_id,
            document=document,
        )

    def edge(source, target, label):
        identity = f"{source}>{target}:{label}"
        edges[identity] = GraphEdge(id=identity, source=source, target=target, label=label)

    def configuration(kind, spec):
        if historical:
            return {
                "document": spec.model_dump(mode="json"),
                "current_revision": spec.revision,
                "id": spec.id,
                "kind": kind,
                "version": 0,
                "draft": None,
            }
        row = db.get(Spec, f"{kind}:{spec.id}")
        value = view(db, row).model_dump(mode="json")
        value["document"] = spec.model_dump(mode="json")
        return value

    for kind, spec in specs:
        if kind == "serving-profile":
            continue
        identity = f"{kind}:{spec.ref}"
        subtitle = spec.ref
        label = getattr(spec, "name", spec.id.replace("-", " ").title())
        if kind == "customer":
            subtitle = (
                f"{spec.tier.replace('_', ' ')} · {spec.workload.task.replace('_', ' ')}"
                f" · r{spec.revision}"
            )
        elif kind == "worker-group":
            total_gpus = spec.desired_instances * spec.hardware.gpu_count
            subtitle = f"{total_gpus} × {spec.hardware.gpu_model} · specification"
        elif kind == "dataset-manifest":
            subtitle = "Prepared artifact" if spec.prepared else "Source manifest · unprepared"
        add(
            identity,
            kind,
            label,
            subtitle,
            "published"
            if historical or db.get(Spec, f"{kind}:{spec.id}").current_revision
            else "draft",
            "configuration",
            spec.id,
            configuration(kind, spec),
        )
        if kind == "customer":
            edge(f"dataset-manifest:{spec.workload.dataset_ref}", identity, "consumes")
        if kind == "scenario":
            for ref in spec.participants:
                edge(f"customer:{ref}", identity, "participates")
                customer_id = ref.split("@")[0]
                warmup = spec.warmup.datasets.get(customer_id)
                if warmup:
                    edge(f"dataset-manifest:{warmup}", f"customer:{ref}", "warmup")
    if not historical:
        from platform_contracts.loading import CONFIG_MODELS

        for row in db.scalars(select(Spec).where(Spec.current_revision == 0)):
            if not row.draft:
                continue
            doc = CONFIG_MODELS[row.kind].model_validate(row.draft)
            add(
                f"{row.kind}:{doc.ref}",
                row.kind,
                getattr(doc, "name", doc.id),
                f"{doc.ref} · unpublished",
                "draft",
                "configuration",
                doc.id,
                configuration(row.kind, doc),
            )
    for binding in bindings:
        profile = profiles[binding.serving_profile_ref]
        identity = "deployment:" + binding.deployment_id
        deployed = runtime.get(binding.deployment_id) if not historical else None
        data = configuration("serving-profile", profile)
        data.update(
            deployment_id=binding.deployment_id, runtime=deployed.payload if deployed else None
        )
        add(
            identity,
            "serving-profile",
            profile.model.repository.split("/")[-1],
            (
                f"{profile.topology.replicas} replica(s) · "
                f"{profile.topology.gpus_per_replica} GPU each"
            ),
            deployed.payload["status"] if deployed else "planned",
            "simulation" if deployed else "configuration",
            profile.id,
            data,
        )
        edge("customer:" + binding.customer_ref, identity, "served by")
        edge(identity, "worker-group:" + binding.worker_group_ref, "placed on")
    if not historical:
        used_profiles = {b.serving_profile_ref for b in bindings}
        for ref, profile in profiles.items():
            if ref not in used_profiles:
                add(
                    "profile:" + ref,
                    "serving-profile",
                    profile.model.repository.split("/")[-1],
                    ref,
                    "published",
                    "configuration",
                    profile.id,
                    configuration("serving-profile", profile),
                )
        for row in runtime.values():
            if row.kind != "worker_group":
                continue
            group = row.payload["group"]
            if not include_history and group["status"]["phase"] in {"deleted", "expired", "failed"}:
                continue
            ref = f"{group['spec']['id']}@{group['spec']['revision']}"
            identity = "group:" + row.id
            state = group["status"]
            add(
                identity,
                "group",
                row.id,
                f"{state['counts']['gpu_ready_nodes']} GPU-ready node(s)",
                state["phase"],
                "simulation",
                row.id,
                row.payload,
            )
            edge("worker-group:" + ref, identity, "allocated as")
            for member in state["members"]:
                mid = "instance:" + member["provider_instance_id"]
                add(
                    mid,
                    "instance",
                    member["provider_instance_id"][-15:],
                    group["spec"]["hardware"]["gpu_model"],
                    member["phase"],
                    "simulation",
                    member["provider_instance_id"],
                    member,
                )
                edge(identity, mid, "contains")
        observed = db.get(Observation, "lambda")
        if observed:
            for instance in observed.payload.get("instances", []):
                add(
                    "lambda:" + instance["id"],
                    "instance",
                    instance.get("name") or instance["id"],
                    "Lambda account · observational",
                    instance.get("status", "unknown"),
                    "live",
                    instance["id"],
                    {**instance, "observed_at": observed.payload.get("observed_at")},
                )
    if not historical:
        from platform_contracts.inventory import NodeObservation

        from platform_management.services.imports import current_inventory_ids
        from platform_management.settings import get_settings

        selected_ids = current_inventory_ids(db)
        # Full and partial records remain distinct; full cluster evidence takes precedence.
        records = list(
            db.scalars(
                select(InventorySnapshot)
                .where(InventorySnapshot.id.in_(selected_ids))
                .order_by(InventorySnapshot.complete)
            )
        )
        for observed_inventory in records:
            doc = observed_inventory.document
            cid = "cluster:" + observed_inventory.cluster_id
            fresh = (
                0
                <= (now() - observed_inventory.captured_at).total_seconds()
                <= get_settings().cluster_observation_max_age_seconds
            )
            provenance = "simulation" if observed_inventory.fixture else "imported"
            add(
                cid,
                "cluster",
                observed_inventory.cluster_id,
                "Infrastructure observation",
                "partial" if not observed_inventory.complete else "observed" if fresh else "stale",
                provenance,
                observed_inventory.cluster_id,
                {
                    "source": observed_inventory.source,
                    "captured_at": observed_inventory.captured_at.isoformat(),
                    "complete_inventory": observed_inventory.complete,
                    **doc.get("cluster", {}),
                },
            )
            for value in doc["nodes"]:
                node = NodeObservation.model_validate(value)
                nid = "infrastructure-node:" + node.provider + ":" + node.provider_instance_id
                readiness = node.readiness.model_dump()
                ready = node.readiness.kubernetes and (
                    node.role == "control_plane" or node.readiness.gpu_runtime
                )
                add(
                    nid,
                    "infrastructure-node",
                    node.kubernetes.name if node.kubernetes else node.id,
                    node.role.replace("_", " ") + " · " + node.provider,
                    "stale" if not fresh else "observed-ready" if ready else "preparing",
                    provenance,
                    node.provider_instance_id,
                    {
                        **value,
                        "source": observed_inventory.source,
                        "captured_at": observed_inventory.captured_at.isoformat(),
                        "complete_inventory": observed_inventory.complete,
                        "readiness": readiness,
                    },
                )
                edge(cid, nid, "observes")
                if node.worker_group_ref:
                    edge("worker-group:" + node.worker_group_ref, nid, "observed worker")
    for row in db.scalars(select(RunRow)):
        run = Run.model_validate(row.payload)
        if historical and row.id != run_id:
            continue
        if not historical and run.status not in {"queued", "running"} and not include_history:
            continue
        identity = "run:" + row.id
        add(
            identity,
            "run",
            run.snapshot.scenario.id.replace("-baseline", ""),
            f"{run.status} · " + (run.result.slo_verdict if run.result else run.stage or "queued"),
            run.status,
            "historical"
            if row.context.get("seed")
            else "simulation"
            if run.fixture
            else "imported",
            row.id,
            {
                **row.payload,
                "_management": {
                    "source": row.context.get("source", "management"),
                    "provenance": row.context.get(
                        "provenance", "synthetic" if run.fixture else "unverified"
                    ),
                },
            },
        )
        edge("scenario:" + run.snapshot.scenario.ref, identity, "executes")
        for dataset in run.snapshot.datasets:
            edge("dataset-manifest:" + dataset.ref, identity, "input to")
        for customer in run.snapshot.customers:
            edge("customer:" + customer.ref, identity, "evaluated in")
    connected = [e for e in edges.values() if e.source in nodes and e.target in nodes]
    counts = {
        "serving_deployments": len({binding.deployment_id for binding in bindings}),
        "worker_group_specs": len(
            {n.resource_id for n in nodes.values() if n.kind == "worker-group"}
        ),
        "customers": len(
            {n.resource_id for n in nodes.values() if n.kind == "customer" and n.status != "draft"}
        ),
        "worker_groups": sum(
            n.kind == "group" and n.status not in {"deleted", "expired", "failed"}
            for n in nodes.values()
        ),
        "planned_gpus": sum(
            p.topology.replicas * p.topology.gpus_per_replica
            for p in {b.deployment_id: profiles[b.serving_profile_ref] for b in bindings}.values()
        ),
        "active_runs": sum(
            n.kind == "run" and n.status in {"queued", "running"} for n in nodes.values()
        ),
    }
    return Topology(
        nodes=list(nodes.values()),
        edges=connected,
        historical=historical,
        captured_at=now(),
        counts=counts,
    )
