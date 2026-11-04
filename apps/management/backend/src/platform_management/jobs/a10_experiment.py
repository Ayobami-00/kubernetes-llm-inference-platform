"""Durable parent workflow. Existing C-owned Jobs execute the ordinary A runner."""

import copy
import json
from datetime import datetime

import httpx
from platform_contracts import (
    ExecutionTarget,
    InfrastructureInventory,
    PreparationManifest,
    Run,
    ServingBinding,
    validate_for_execution,
)
from platform_contracts.runs import ExecutionVersions
from workload_runner.artifacts import read_jsonl
from workload_runner.experiments.a10 import (
    DEPLOYMENT,
    ENDPOINT,
    GROUP,
    NODE,
    schedule_identity,
)
from workload_runner.scenario_loader import compile_run

from platform_management.api.schemas import RunLaunch
from platform_management.db.models import (
    ArtifactCheck,
    InventorySnapshot,
    PreparedRun,
    Resource,
    RunRow,
)
from platform_management.db.session import session
from platform_management.integrations.a10_serving import A10Serving, ready
from platform_management.integrations.job_runner import frozen_file
from platform_management.integrations.observations import refresh_cluster
from platform_management.jobs.queue import owned, submit
from platform_management.services.a10_experiments import (
    assert_run_access,
    directory,
    restored_catalog,
)
from platform_management.services.common import AppError, digest, now
from platform_management.services.imports import inspect_artifacts
from platform_management.services.readiness import assess

TERMINAL = {"completed", "failed", "cancelled"}


def save(path, value):
    frozen_file(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


def compile_variant(frozen, variant, output, inventory):
    catalog = restored_catalog(frozen)
    spec = catalog.records["worker-group"]
    profile = frozen["profiles"][variant]
    return compile_run(
        catalog,
        next(iter(catalog.records["scenario"])),
        {"writer-assist@1": PreparationManifest.model_validate(frozen["preparation"])},
        ExecutionTarget(
            mode="benchmark", endpoint=ENDPOINT, credential_env={"writer-assist": "INFERENCE_TOKEN"}
        ),
        output,
        bindings=(
            ServingBinding(
                customer_ref="writer-assist@1",
                serving_profile_ref=f"{profile['id']}@{profile['revision']}",
                worker_group_ref=next(iter(spec)),
                deployment_id=DEPLOYMENT,
            ),
        ),
        inventory=InfrastructureInventory.model_validate(inventory),
        versions=ExecutionVersions.model_validate(frozen["versions"]),
        duration_variant="paired-a10-5m",
    )


def start_child(data, variant, checkpoint, config, evidence):
    """Fresh measured readiness precedes snapshot freeze; no launch-time rewriting."""
    with session() as db:
        parent = db.get(Resource, data["id"])
        existing = parent.payload["children"].get(variant)
        if existing:
            child = db.get(RunRow, existing)
            if not child or child.context.get("parent_experiment") != data["id"]:
                raise ValueError("Persisted child ownership differs")
            return existing
    root = directory(data["id"])
    frozen = data["definition"]
    stamp = now()
    inventory = copy.deepcopy(frozen["inventory_template"])
    inventory.update(snapshot_id=data["id"] + "-" + variant, captured_at=stamp.isoformat())
    gpu_node = next(n for n in inventory["nodes"] if n["id"] == NODE)
    gpu_node["observed_at"] = stamp.isoformat()
    # Fresh facts below come from Node readiness/GPU allocation, Cilium Pod readiness,
    # and an authenticated CPU-to-GPU gateway probe, not a declaration-only import.
    gpu_node["readiness"] = dict.fromkeys(
        ("host", "wireguard", "kubernetes", "cilium", "gpu_runtime"), True
    )
    inv = InfrastructureInventory.model_validate(inventory)
    snap = compile_variant(frozen, variant, root / variant / "plan", inventory)
    schedule = list(read_jsonl(root / variant / "plan/admission-schedule.jsonl"))
    if schedule_identity(schedule) != checkpoint["schedule_sha256"]:
        raise ValueError("Child selection/admissions differ from the frozen paired plan")
    validate_for_execution(snap)
    save(root / variant / "readiness.json", evidence)
    checks = inspect_artifacts(snap.model_dump(mode="json"))
    if any(check["status"] != "verified" for check in checks):
        raise ValueError("A child execution artifact failed checksum verification")
    refresh_cluster()
    key = data["id"] + "-" + variant
    run_id = "run-" + digest(key)[:16]
    with session() as db, db.begin():
        parent = db.get(Resource, data["id"], with_for_update=True)
        if parent.payload.get("stop_requested"):
            return None
        deployment = db.get(Resource, DEPLOYMENT, with_for_update=True)
        if deployment.active_operation_id != data["operation_id"]:
            raise ValueError("Experiment lost its deployment reservation")
        group = db.get(Resource, GROUP, with_for_update=True)
        if group.payload.get("ownership") != "external":
            raise ValueError("Retained ownership changed")
        group_payload = copy.deepcopy(group.payload)
        group_payload["inventory_id"] = inv.snapshot_id
        group_payload["group"]["status"]["observed_at"] = stamp.isoformat()
        group.payload = group_payload
        deployment.payload = {
            **deployment.payload,
            "profile": frozen["profiles"][variant],
            "status": "ready",
        }
        if not db.get(InventorySnapshot, inv.snapshot_id):
            db.add(
                InventorySnapshot(
                    id=inv.snapshot_id,
                    cluster_id=inv.cluster.id,
                    captured_at=stamp,
                    document=inventory,
                    digest=digest(inventory),
                    source=data["id"] + ": measured Kubernetes/Cilium/gateway evidence",
                    complete=True,
                    fixture=False,
                )
            )
        prepared_id = "prepared-run-" + digest(snap.model_dump(mode="json"))[:24]
        if not db.get(PreparedRun, prepared_id):
            db.add(
                PreparedRun(
                    id=prepared_id,
                    digest=digest(snap.model_dump(mode="json")),
                    snapshot=snap.model_dump(mode="json"),
                    fixture=False,
                    source=data["id"],
                )
            )
        for check in checks:
            db.merge(ArtifactCheck(**check))
        db.flush()
        launch = RunLaunch(
            scenario_ref=snap.scenario.ref,
            prepared_run_id=prepared_id,
            mode="real",
            group_ids={snap.worker_groups[0].ref: GROUP},
        )

        def mutate(operation_id):
            assert_run_access(db, snap, data["operation_id"])
            preflight = assess(db, launch, snap)
            if not preflight.executable:
                raise ValueError(
                    "Child preflight blocked: "
                    + "; ".join(preflight.contract_blockers + preflight.runtime_blockers)
                )
            run = Run(id=run_id, fixture=False, status="queued", created_at=stamp, snapshot=snap)
            db.add(
                RunRow(
                    id=run_id,
                    binding_id=parent.binding_id,
                    snapshot=snap.model_dump(mode="json"),
                    payload=run.model_dump(mode="json"),
                    context={
                        "mode": "real",
                        "outcome": "passing",
                        "group_ids": launch.group_ids,
                        "source": data["id"],
                        "provenance": "measured",
                        "prepared_run_id": prepared_id,
                        "parent_experiment": data["id"],
                    },
                )
            )

        accepted = submit(db, key, "start_run", run_id, launch.model_dump(mode="json"), mutate)
        # Persist the association in the same transaction as child admission.
        parent.payload = {
            **parent.payload,
            "children": {**parent.payload["children"], variant: accepted["resource_id"]},
        }
    return run_id


def connectivity(config):
    from pathlib import Path

    secret = Path("/run/secrets/runner/klip-writer-inference/api-key").read_text().strip()
    if not secret:
        raise ValueError("Inference credential file is empty")
    # Exactly one unscored attempt. No secret or prompt appears in retained evidence.
    with httpx.Client(timeout=10, trust_env=False) as client:
        payload = {
            "model": "gec-small",
            "messages": [{"role": "user", "content": "Correct this sentence: She go home."}],
            "max_tokens": 16,
            "temperature": 0,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        missing = client.post(ENDPOINT, json=payload)
        wrong = client.post(
            ENDPOINT,
            headers={"Authorization": "Bearer klip-invalid-experiment-probe"},
            json=payload,
        )
        unknown = client.post(
            ENDPOINT,
            headers={"Authorization": "Bearer " + secret},
            json={**payload, "model": "klip-unknown-experiment-model"},
        )
        if (missing.status_code, wrong.status_code, unknown.status_code) != (401, 401, 404):
            raise ValueError("Private gateway authentication or fixed-model routing failed")
        response = client.post(
            ENDPOINT,
            headers={"Authorization": "Bearer " + secret},
            json=payload,
        )
    if response.status_code != 200 or not response.json().get("choices"):
        raise ValueError(
            f"Unscored authenticated readiness probe failed: HTTP {response.status_code}"
        )
    return {
        "observed_at": now().isoformat(),
        "status": 200,
        "credential_source": "mounted-secret-file",
        "scored": False,
        "missing_key": 401,
        "wrong_key": 401,
        "unknown_model": 404,
    }


def fresh_evidence(actuator, data, variant):
    is_ready, evidence = actuator.observe(
        data["operation_id"], data["definition"]["profiles"][variant]
    )
    if not is_ready:
        return None
    cilium = actuator.client.request(
        "GET",
        "api/v1/namespaces/kube-system/pods?labelSelector=k8s-app%3Dcilium&fieldSelector=spec.nodeName%3D"
        + NODE,
    )["items"]
    if len(cilium) != 1 or not ready(cilium[0]):
        raise ValueError("Retained node's Cilium agent is not ready")
    evidence["cilium_pod_uid"] = cilium[0]["metadata"]["uid"]
    evidence["client_node"] = actuator.client_node(data["definition"]["client_placement"])
    evidence["gateway"] = actuator.gateway()
    evidence["connectivity"] = connectivity(actuator.binding)
    return evidence


def stop_children(data):
    with session() as db, db.begin():
        pending = False
        parent = db.get(Resource, data["id"])
        for run_id in parent.payload["children"].values():
            row = db.get(RunRow, run_id, with_for_update=True)
            if row.payload["status"] not in TERMINAL:
                row.context = {**row.context, "stop_requested": True}
                pending = True
        return pending


def finish(data, actuator, status):
    from platform_management.services.a10_reporting import terminal_artifacts

    live = actuator.release(data["operation_id"])
    root = directory(data["id"])
    saved = root / "terminal-state.json"
    if saved.exists():
        terminal = json.loads(saved.read_text())
        if (
            terminal["definition_sha256"] != data["definition_sha256"]
            or terminal["status"] != status
        ):
            raise ValueError("Terminal experiment identity or outcome changed")
        data = terminal
    else:
        data.update(status=status, stage=status, finished_at=now().isoformat())
    artifacts = terminal_artifacts(data, root)
    if status != "completed" and data.get("artifacts", {}).get("report"):
        data["artifacts"]["comparison_report"] = data["artifacts"]["report"]
    data["artifacts"] = {**data.get("artifacts", {}), **artifacts}
    with session() as db, db.begin():
        deployment = db.get(Resource, DEPLOYMENT, with_for_update=True)
        if deployment.active_operation_id == data["operation_id"]:
            if live:
                ref = live["metadata"].get("annotations", {}).get("klip.dev/serving-profile")
                profile = next(
                    (
                        p
                        for p in data["definition"]["profiles"].values()
                        if f"{p['id']}@{p['revision']}" == ref
                    ),
                    None,
                )
                if profile:
                    observed = live.get("status", {})
                    available = (
                        observed.get("observedGeneration", 0) >= live["metadata"]["generation"]
                        and observed.get("readyReplicas") == 1
                    )
                    deployment.payload = {
                        **deployment.payload,
                        "profile": profile,
                        "status": "ready" if available else "unavailable",
                    }
            deployment.active_operation_id = None
    return data


def step_experiment(op, payload, checkpoint, config, owner=None):
    data = copy.deepcopy(payload)
    if op["type"] == "stop_experiment":
        return data, checkpoint, data["status"] in TERMINAL, False, "Cancellation requested"
    data.update(status="running", started_at=data.get("started_at") or now().isoformat())
    with session() as db:
        current = db.get(Resource, data["id"])
        data["stop_requested"] = bool(current.payload.get("stop_requested"))
        data["children"] = {**data["children"], **current.payload.get("children", {})}
    actuator = A10Serving({**config, "a10_deployment_uid": data["definition"]["deployment_uid"]})
    try:
        stage = checkpoint.get("step", "preflight")
        data["stages"].setdefault(stage, {"started_at": now().isoformat()})
        if data["stop_requested"] or checkpoint.get("failure"):
            if stop_children(data):
                data["stage"] = "cancelling"
                return (
                    data,
                    checkpoint,
                    False,
                    False,
                    "Waiting for child cancellation and partial artifacts",
                )
            status = "failed" if checkpoint.get("failure") else "cancelled"
            return finish(data, actuator, status), checkpoint, True, status == "failed", status
        if digest(data["definition"]) != data["definition_sha256"]:
            raise ValueError("Frozen experiment definition checksum differs")
        if any(
            datetime.fromisoformat(review["expires_at"]) <= now()
            for review in data["definition"]["risk_reviews"].values()
        ):
            raise ValueError("A frozen image risk review expired; stop and retain evidence")
        if (now() - datetime.fromisoformat(data["started_at"])).total_seconds() > 1500:
            raise ValueError("Paired experiment exceeded its explicit 25-minute safety watchdog")
        root = directory(data["id"])
        if stage == "preflight":
            actuator.node()
            actuator.client_node(data["definition"]["client_placement"])
            gateway = actuator.gateway()
            save(root / "definition.json", data["definition"])
            save(root / "initial-gateway.json", gateway)
            template = compile_variant(
                data["definition"],
                "baseline",
                root / "paired-template",
                data["definition"]["inventory_template"],
            )
            rows = list(read_jsonl(root / "paired-template/admission-schedule.jsonl"))
            if sum(not row["warmup"] for row in rows) < 100:
                raise ValueError(
                    "Materialized schedule cannot satisfy the unchanged 100-sample requirement"
                )
            checkpoint["schedule_sha256"] = schedule_identity(rows)
            checkpoint["template_snapshot_sha256"] = digest(template.model_dump(mode="json"))
            checkpoint["step"] = "baseline-activate"
        elif stage.endswith("-activate"):
            variant = stage.removesuffix("-activate")
            if checkpoint.get("activation_variant") != variant:
                checkpoint.update(
                    activation_variant=variant, activation_started_at=now().isoformat()
                )
                with session() as db, db.begin():
                    job = owned(db, op["id"], owner)
                    job.checkpoint = copy.deepcopy(checkpoint)
            actuator.activate(data["operation_id"], data["definition"]["profiles"][variant])
            checkpoint.update(step=variant + "-ready")
        elif stage.endswith("-ready"):
            variant = stage.removesuffix("-ready")
            if variant in data["children"]:
                # Admission was committed before a worker died; observe that child.
                # Do not probe, recompile, refresh evidence or inject extra traffic.
                checkpoint["step"] = variant + "-run"
                data["stage"] = checkpoint["step"]
                data["stages"][stage]["finished_at"] = now().isoformat()
                return data, checkpoint, False, False, "Recovered original child run"
            if (
                now() - datetime.fromisoformat(checkpoint["activation_started_at"])
            ).total_seconds() > 240:
                raise ValueError("Serving activation exceeded the four-minute readiness watchdog")
            evidence = fresh_evidence(actuator, data, variant)
            if evidence:
                data["stages"][stage]["serving_pod_uid"] = evidence["pods"][0]["uid"]
                child = start_child(data, variant, checkpoint, config, evidence)
                if child:
                    data["children"][variant] = child
                    checkpoint["step"] = variant + "-run"
        elif stage.endswith("-run"):
            variant = stage.removesuffix("-run")
            is_ready, serving = actuator.observe(
                data["operation_id"], data["definition"]["profiles"][variant]
            )
            if not is_ready:
                raise ValueError("Serving readiness was lost during the child execution")
            prior_uid = data["stages"].get(variant + "-ready", {}).get("serving_pod_uid")
            if prior_uid and serving["pods"][0]["uid"] != prior_uid:
                raise ValueError("Model Pod identity changed during the child execution")
            with session() as db:
                child = db.get(RunRow, data["children"][variant])
                state = child.payload["status"]
                child_handle = child.context.get("handle")
                data["stages"][stage].update(
                    child_stage=child.payload.get("stage"),
                    timeline_origin_at=child.context.get("timeline_origin_at"),
                    job_uid=child.context.get("job_uid"),
                )
            if child_handle and state not in TERMINAL:
                pods = actuator.client.pods("klip-" + child_handle)
                assigned = [pod for pod in pods if pod["spec"].get("nodeName")]
                if any(
                    pod["spec"]["nodeName"] != data["definition"]["client_placement"]["name"]
                    for pod in assigned
                ):
                    raise ValueError("Runner Pod was assigned outside the frozen CPU placement")
                if assigned:
                    data["stages"][stage]["client_pods"] = [
                        {"uid": pod["metadata"]["uid"], "node_name": pod["spec"]["nodeName"]}
                        for pod in assigned
                    ]
            if state in TERMINAL:
                if state != "completed":
                    raise ValueError(
                        f"{variant} child execution ended as {state}; no automatic replay"
                    )
                checkpoint["step"] = "ngram-activate" if variant == "baseline" else "comparison"
        elif stage == "comparison":
            from platform_management.services.a10_reporting import build_comparison

            comparison, artifacts = build_comparison(data, root)
            data.update(comparison=comparison, artifacts=artifacts)
            return (
                finish(data, actuator, "completed"),
                checkpoint,
                True,
                False,
                "Comparison artifacts verified",
            )
        else:
            raise ValueError("Unknown experiment stage")
        if stage not in data["stages"]:
            data["stages"][stage] = {"started_at": now().isoformat()}
        if checkpoint["step"] != stage:
            data["stages"][stage]["finished_at"] = now().isoformat()
        data["stage"] = checkpoint["step"]
        return data, checkpoint, False, False, data["stage"]
    except (ValueError, AppError, OSError) as exc:
        error = {"code": "experiment_failed", "message": str(exc)[:500]}
        data.update(error=error, stage="cleanup")
        checkpoint.update(failure=error, step="cleanup")
        return data, checkpoint, False, False, "Failure recorded; preserving child artifacts"
    finally:
        actuator.close()
