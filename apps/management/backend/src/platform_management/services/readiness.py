"""Readiness consumes evidence. It does not schedule traffic or evaluate SLOs."""

from datetime import datetime

from platform_contracts import validate_for_execution
from platform_contracts.common import is_image_pin

from platform_management.api.schemas import Preflight, ReadinessCheck
from platform_management.db.models import ArtifactCheck, Observation, PreparedRun, Resource
from platform_management.integrations.registry import configuration
from platform_management.services.common import digest, now
from platform_management.services.imports import artifact_references
from platform_management.settings import get_settings


def assess(db, body, snap):
    checks = []

    def check(code, label, ready, detail, ref=None, state="blocked"):
        checks.append(
            ReadinessCheck(
                code=code,
                label=label,
                status="ready" if ready else state,
                detail=detail,
                resource_ref=ref,
            )
        )

    errors = []
    try:
        validate_for_execution(snap)
    except ValueError as exc:
        errors = str(exc).split("\n- ")[1:] or [str(exc)]
    check(
        "contract-completeness",
        "Execution contract",
        not errors,
        "\n".join(errors) if errors else "Shared execution validation passed",
    )
    prepared = db.get(PreparedRun, body.prepared_run_id) if body.prepared_run_id else None
    check(
        "prepared-run",
        "Frozen prepared bundle",
        prepared is not None,
        "Select an imported final execution snapshot from A",
    )
    check(
        "measurement-source",
        "Measured execution inputs",
        bool(prepared and not prepared.fixture),
        "Synthetic prepared bundles are restricted to explicit simulation",
    )

    real = configuration("real")
    runner_ready = (
        real["runner"] == "kubernetes:v1"
        and is_image_pin(real["runner_image"])
        and bool(real["runner_command"])
        and real["runner_image"] == snap.versions.runner_image
    )
    check(
        "runner-adapter",
        "Real runner",
        runner_ready,
        "Configure A's pinned runner image and execute-only command",
        state="unconfigured",
    )
    check(
        "artifact-storage",
        "Shared artifact storage",
        bool(real["artifact_claim"]),
        "A shared artifact PVC and consistent artifact root are required",
        state="unconfigured",
    )

    for identity, (uri, sha) in artifact_references(snap.model_dump(mode="json")).items():
        row = db.get(ArtifactCheck, identity)
        check(
            "artifact-" + identity[:12],
            "Artifact checksum",
            bool(row and row.status == "verified" and sha),
            row.error
            if row and row.error
            else "Artifact must be staged and checksummed before execution",
            uri,
        )

    inv = snap.inventory
    inventory_ready = bool(inv and not inv.fixture)
    check(
        "inventory",
        "Full infrastructure inventory",
        inventory_ready,
        "B must supply a complete non-fixture inventory; foundation allocations are partial",
    )
    if inv:
        fresh = (
            0
            <= (now() - inv.captured_at).total_seconds()
            <= get_settings().cluster_observation_max_age_seconds
        )
        check(
            "inventory-freshness",
            "Inventory observation age",
            fresh,
            "Imported node evidence exceeds its freshness window"
            if not fresh
            else "Inventory is current",
            inv.snapshot_id,
            state="stale",
        )
    cluster = db.get(Observation, "cluster-readiness")
    observation = cluster.payload if cluster else {}
    observed = observation.get("observed_at")
    cluster_fresh = bool(
        observed
        and 0
        <= (now() - datetime.fromisoformat(observed)).total_seconds()
        <= get_settings().cluster_observation_max_age_seconds
    )
    cluster_ready = cluster_fresh and observation.get("status") == "fresh"
    # Changing cluster configuration must not reroute a frozen execution.
    if inv:
        endpoint = real.get("kube_api_url", "")
        if endpoint and endpoint.rstrip("/") != str(inv.cluster.api_endpoint).rstrip("/"):
            cluster_ready = False
    check(
        "cluster-api",
        "Live Kubernetes observations",
        cluster_ready,
        observation.get("error") or "Read-only cluster observations must be current",
        state="stale" if observed else "unconfigured",
    )

    sim_blockers = []
    for spec in snap.worker_groups:
        identity = body.group_ids.get(spec.ref)
        row = db.get(Resource, identity) if identity else None
        matches = bool(
            row
            and row.kind == "worker_group"
            and row.payload["group"]["spec"] == spec.model_dump(mode="json")
        )
        state = row.payload["group"]["status"] if matches else {}
        sim_ready = matches and state.get("phase") == "ready" and not row.active_operation_id
        if not sim_ready:
            sim_blockers.append(
                f"{spec.ref}: select a ready runtime group using this exact specification"
            )
        minimum = (
            snap.scenario.warmup.duration_s
            + snap.scenario.measurement_duration_s
            + snap.scenario.drain_timeout_s
            + spec.drain_timeout_s
            + get_settings().runner_startup_allowance_seconds
        )
        lease_ready = bool(
            sim_ready
            and state.get("expires_at")
            and (datetime.fromisoformat(state["expires_at"]) - now()).total_seconds() >= minimum
        )
        real_group = bool(
            matches
            and row.payload.get("mode") == "real"
            and (
                not snap.target
                or snap.target.serving_stack != "direct-vllm"
                or row.payload.get("endpoint") == str(snap.target.endpoint)
            )
            and not row.payload["group"].get("fixture")
        )
        if real_group and row.payload.get("ownership") == "external":
            real_group = row.payload.get("inventory_id") == (inv.snapshot_id if inv else None)
        check(
            "lease-" + spec.id,
            "Observed capacity and remaining lease",
            real_group and lease_ready,
            "Real execution requires matching measured capacity with enough remaining lease",
            spec.ref,
        )
        nodes = [n for n in inv.nodes if n.worker_group_ref == spec.ref] if inv else []
        live_nodes = {n["uid"]: n for n in observation.get("nodes", [])}
        gpu_ready = len(nodes) >= spec.desired_instances and all(
            0
            <= (now() - n.observed_at).total_seconds()
            <= get_settings().cluster_observation_max_age_seconds
            and n.readiness.host
            and n.readiness.wireguard
            and n.readiness.kubernetes
            and n.readiness.cilium
            and n.readiness.gpu_runtime
            and n.kubernetes
            and n.kubernetes.uid in live_nodes
            and live_nodes[n.kubernetes.uid]["ready"]
            and live_nodes[n.kubernetes.uid]["gpus"] >= spec.hardware.gpu_count
            for n in nodes
        )
        members = {m.get("node_uid"): m for m in state.get("members", [])}
        if real_group:
            gpu_ready = gpu_ready and all(
                n.kubernetes
                and n.kubernetes.uid in members
                and members[n.kubernetes.uid]["provider_instance_id"] == n.provider_instance_id
                for n in nodes
            )
        check(
            "gpu-" + spec.id,
            "GPU worker evidence",
            gpu_ready,
            "Host, mesh, CNI and GPU readiness need distinct evidence",
            spec.ref,
        )
    live_deployments = {d["id"]: d for d in observation.get("deployments", [])}
    for binding in snap.bindings:
        row = db.get(Resource, binding.deployment_id)
        profile = next(p for p in snap.serving_profiles if p.ref == binding.serving_profile_ref)
        sim_ready = bool(
            row
            and row.kind == "deployment"
            and row.payload["status"] == "ready"
            and row.payload["profile"] == profile.model_dump(mode="json")
            and row.payload["group_id"] == body.group_ids.get(binding.worker_group_ref)
        )
        if not sim_ready:
            sim_blockers.append(
                f"{binding.deployment_id}: serving deployment differs or is not ready"
            )
        live = live_deployments.get(binding.deployment_id, {})
        check(
            "serving-" + binding.deployment_id,
            "Serving readiness",
            cluster_ready
            and sim_ready
            and row.payload.get("mode") == "real"
            and live.get("ready", False)
            and live.get("profile_ref") == profile.ref
            and (
                not row.payload.get("kubernetes_uid")
                or live.get("uid") == row.payload["kubernetes_uid"]
            )
            and profile.engine.image in live.get("images", [])
            and live.get("profile_sha256") == digest(profile.model_dump(mode="json"))
            and live.get("replicas", 0) >= profile.topology.replicas,
            "Deployment readiness and exact profile identity must be observed separately",
            binding.deployment_id,
        )
    # Keep legacy fields while replacing the old blanket blocker with evidence-specific checks.
    runtime = [
        c.detail + (": " + c.resource_ref if c.resource_ref else "")
        for c in checks
        if c.status != "ready" and c.code != "contract-completeness"
    ]
    return Preflight(
        executable=all(c.status == "ready" for c in checks),
        contract_blockers=errors,
        runtime_blockers=runtime,
        simulation_available=not sim_blockers
        or bool(
            prepared
            and prepared.fixture
            and snap.target
            and snap.target.mode == "local-stub"
            and configuration()["runner"] == "kubernetes-workload-stub:v1"
        ),
        checks=checks,
    )
