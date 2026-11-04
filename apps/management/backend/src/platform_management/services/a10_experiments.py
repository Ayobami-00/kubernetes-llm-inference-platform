"""Frozen reviewed inputs and durable ownership for the one retained A10."""

import copy
import json

from platform_contracts import InfrastructureInventory, PreparationManifest, ServingProfile
from platform_contracts.loading import CONFIG_MODELS, load_document
from sqlalchemy import select, text
from workload_runner.experiments.a10 import (
    BOOT_ID,
    DEPLOYMENT,
    ENDPOINT,
    GROUP,
    INSTANCE,
    NODE,
    NODE_UID,
    VARIANTS,
    comparison_scenario,
    validate_profiles,
)

from platform_management.db.models import InventorySnapshot, Observation, Resource, RunRow
from platform_management.integrations.artifacts import LocalArtifactAccess
from platform_management.integrations.registry import configuration
from platform_management.services.catalog import catalog
from platform_management.services.common import AppError, lock_key, now
from platform_management.settings import get_settings


def definition(db):
    """Freeze published source contracts, source artifacts and exact runtime pins."""
    settings = get_settings()
    if not settings.a10_experiment_enabled:
        raise AppError(422, "The retained A10 experiment is not enabled in this environment")
    config = configuration("real")
    if config["runner"] != "kubernetes:v1" or not config["artifact_claim"]:
        raise AppError(422, "Configure the real pinned runner and shared artifact volume first")
    cpu_name = config["runner_node_selector"].get("kubernetes.io/hostname")
    cluster = db.get(Observation, "cluster-readiness")
    cpu = (
        next((node for node in cluster.payload.get("nodes", []) if node["name"] == cpu_name), None)
        if cluster
        else None
    )
    from datetime import datetime

    fresh = bool(
        cluster
        and cluster.payload.get("status") == "fresh"
        and cluster.payload.get("observed_at")
        and 0
        <= (now() - datetime.fromisoformat(cluster.payload["observed_at"])).total_seconds()
        <= settings.cluster_observation_max_age_seconds
    )
    if not fresh or not cpu or not cpu["ready"] or cpu.get("gpus") != 0:
        raise AppError(422, "Pin one ready persistent CPU worker for both workload-generator Jobs")
    cat = catalog(db)
    preparation_path = settings.a10_preparation_file.resolve()
    if not preparation_path.is_relative_to(settings.artifacts_root.resolve()):
        raise AppError(422, "Prepared JFLEG must be on the shared artifact mount")
    preparation = PreparationManifest.model_validate_json(preparation_path.read_text())
    tokenizer_metadata = preparation_path.parents[2] / "tokenizer-metadata.json"
    tokenizer = json.loads(tokenizer_metadata.read_text())
    if (
        tokenizer.get("repository") != preparation.tokenizer_id
        or tokenizer.get("revision") != preparation.tokenizer_revision
        or not LocalArtifactAccess(settings.artifacts_root).verify(
            tokenizer["artifact"]["uri"], tokenizer["artifact"]["sha256"]
        )
    ):
        raise AppError(422, "Stage the checksum-verified pinned tokenizer for copy-span analysis")
    if (
        preparation.customer_ref != "writer-assist@1"
        or preparation.data_kind != "public"
        or preparation.token_accounting != "model_tokenizer"
        or not all(preparation.licenses.values())
    ):
        raise AppError(422, "The paired experiment requires licensed public WriterAssist inputs")
    if not LocalArtifactAccess(settings.artifacts_root).verify(
        preparation.artifact.uri, preparation.artifact.sha256
    ):
        raise AppError(422, "Prepared JFLEG artifact is missing or its checksum differs")
    base = settings.repository_root / "deploy/environments/lab/experiments/a10"
    profiles = {
        variant: load_document(base / f"{variant}.yaml", ServingProfile).model_dump(mode="json")
        for variant in VARIANTS
    }
    validate_profiles(profiles)
    reviews = {}
    for name in ("model", "gateway"):
        review = json.loads(
            (settings.repository_root / f"deploy/retained-a10-{name}-risk-review.json").read_text()
        )
        if datetime.fromisoformat(review["expires_at"]) <= now():
            raise AppError(422, f"The retained A10 {name} risk review has expired")
        if name == "model" and review["image"] != profiles["baseline"]["engine"]["image"]:
            raise AppError(422, "The serving image differs from the model risk review")
        reviews[name] = review
    inventory = next(
        (
            InfrastructureInventory.model_validate(row.document)
            for row in db.scalars(
                select(InventorySnapshot)
                .where(InventorySnapshot.complete.is_(True), InventorySnapshot.fixture.is_(False))
                .order_by(InventorySnapshot.captured_at.desc())
            )
            if any(n.get("id") == NODE for n in row.document.get("nodes", []))
        ),
        None,
    )
    group = db.get(Resource, GROUP)
    deployment = db.get(Resource, DEPLOYMENT)
    if (
        not inventory
        or not group
        or not deployment
        or group.payload.get("ownership") != "external"
        or deployment.payload.get("ownership") != "external"
        or group.payload["group"]["spec"]["capacity_lifecycle"] != "externally_retained"
        or group.payload["group"]["status"].get("expires_at") is not None
    ):
        raise AppError(422, "Import the externally retained A10 capacity before experimenting")
    node = next(n for n in inventory.nodes if n.id == NODE)
    if (
        node.provider_instance_id != INSTANCE
        or not node.kubernetes
        or node.kubernetes.uid != NODE_UID
    ):
        raise AppError(422, "The retained GPU identity differs")
    selected = {
        "customer": [cat.get("customer", "writer-assist@1").model_dump(mode="json")],
        "scenario": [
            comparison_scenario(cat.get("scenario", "writer-assist-baseline@1")).model_dump(
                mode="json"
            )
        ],
        "dataset-manifest": [
            cat.get("dataset-manifest", ref).model_dump(mode="json")
            for ref in preparation.source_manifest_refs
        ],
        "worker-group": [copy.deepcopy(group.payload["group"]["spec"])],
        "serving-profile": list(profiles.values()),
    }
    summary = {
        "name": "WriterAssist · A10 baseline vs n-gram",
        "estimated_minutes": 20,
        "warmup_seconds_per_variant": 60,
        "scored_seconds_per_variant": 300,
        "maximum_drain_seconds_per_variant": 30,
        "rates_per_second": [0.5, 1.5, 0.5],
        "seed": 42,
        "node": NODE,
        "client_node": {"name": cpu_name, "uid": cpu["uid"]},
        "node_uid": NODE_UID,
        "boot_id": BOOT_ID,
        "source": "JFLEG development warmup / test evaluation; four references",
        "preparation_id": preparation.id,
        "model": profiles["baseline"]["model"],
        "speculation": profiles["ngram"]["engine"]["speculative_decoding"],
        "slo": selected["customer"][0]["slo"],
        "order": list(VARIANTS),
        "limitations": "One baseline-first pair; private single-GPU experiment, no serving HA.",
    }
    return {
        "schema_version": "a10-paired.v1",
        "summary": summary,
        "catalog": selected,
        "profiles": profiles,
        "preparation": preparation.model_dump(mode="json"),
        "analysis_tokenizer": tokenizer["artifact"],
        "risk_reviews": reviews,
        "report_template": (
            settings.repository_root / "experiments/writer-assist-a10/ngram-comparison.md"
        ).read_text(),
        "inventory_template": inventory.model_dump(mode="json"),
        "deployment_uid": deployment.payload["kubernetes_uid"],
        "client_placement": {
            "name": cpu_name,
            "uid": cpu["uid"],
            "node_selector": config["runner_node_selector"],
            "resources": config["runner_resources"],
        },
        "group_template": copy.deepcopy(group.payload),
        "endpoint": ENDPOINT,
        "versions": {
            "runner_image": config["runner_image"],
            "aiperf_version": "0.13.0",
            "evaluator_version": "0.1.0",
            "platform_versions": {
                "kubernetes": "1.36.5",
                "cilium": "1.20.4",
                "vllm": "0.32.0",
                "llm-d": "1.0.0",
                "model-pool-chart": "1.0.0",
                "serving-chart": "0.1.1",
            },
        },
    }


def reserve(db, operation_id):
    """Serialize normal run admission, experiment admission and capacity imports."""
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key("resource:" + DEPLOYMENT)})
    row = db.get(Resource, DEPLOYMENT, with_for_update=True)
    if not row or row.active_operation_id:
        raise AppError(409, "The A10 deployment is already reserved", "resource_busy")
    for run in db.scalars(select(RunRow)):
        if run.payload["status"] not in {"completed", "failed", "cancelled"} and any(
            b["deployment_id"] == DEPLOYMENT for b in run.snapshot.get("bindings", [])
        ):
            raise AppError(409, "An existing run is using the A10 deployment", "resource_busy")
    row.active_operation_id = operation_id


def assert_run_access(db, snapshot, parent_operation=None):
    for binding in sorted(snapshot.bindings, key=lambda b: b.deployment_id):
        db.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": lock_key("resource:" + binding.deployment_id)},
        )
        row = db.get(Resource, binding.deployment_id, with_for_update=True)
        if row and row.active_operation_id not in {None, parent_operation}:
            raise AppError(
                409, "The serving deployment is reserved by an experiment", "resource_busy"
            )


def restored_catalog(frozen):
    from platform_contracts import ContractCatalog

    result = ContractCatalog()
    for kind, rows in frozen["catalog"].items():
        for value in rows:
            result.add(kind, CONFIG_MODELS[kind].model_validate(value))
    return result


def directory(identity):
    if not identity.startswith("experiment-") or any(
        c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in identity
    ):
        raise ValueError("Invalid experiment identity")
    return get_settings().artifacts_root.resolve() / "experiments" / "a10-pairs" / identity
