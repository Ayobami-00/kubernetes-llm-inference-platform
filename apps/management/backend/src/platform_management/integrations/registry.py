"""Versioned, immutable adapter selection. Bindings contain references, never secret values."""

from platform_contracts.common import is_image_pin
from sqlalchemy.dialects.postgresql import insert

from platform_management.db.models import ExecutionBinding
from platform_management.services.common import AppError, digest
from platform_management.settings import get_settings

SIMULATED = "simulated:v1"


def configuration(mode="simulation"):
    s = get_settings()
    result = {
        "mode": mode,
        **{
            k: SIMULATED if mode == "simulation" else "unconfigured:v1"
            for k in ("provider", "host", "cluster", "serving")
        },
        "runner": s.simulation_runner if mode == "simulation" else s.real_runner,
        "runner_image": s.runner_image,
        "runner_command": s.runner_command,
        "runner_resources": s.runner_resources,
        "runner_node_selector": s.runner_node_selector,
        "runner_tolerations": s.runner_tolerations,
        "namespace": s.runner_namespace,
        "serving_namespace": s.serving_namespace,
        "service_account": s.runner_service_account,
        "runner_secret_refs": s.runner_secret_refs,
        "artifact_root": str(s.artifacts_root.resolve()),
        "artifact_claim": s.artifact_claim,
        "startup_allowance_seconds": s.runner_startup_allowance_seconds,
        "finalization_allowance_seconds": 120,
        "kube_api_url": s.kube_api_url,
        "kube_ca_file": str(s.kube_ca_file),
        "kube_token_file": str(s.kube_token_file),
        "kube_allow_insecure_local": s.kube_allow_insecure_local,
    }
    if mode == "real":
        from platform_management.integrations.lambda_policy import configuration as lifecycle

        result.update(lifecycle(s))
    return result


def bind(db, mode="simulation"):
    payload = configuration(mode)
    # Freeze the in-cluster endpoint too; recovery must not follow a new Pod's environment.
    import os

    if not payload["kube_api_url"] and os.environ.get("KUBERNETES_SERVICE_HOST"):
        payload["kube_api_url"] = (
            "https://"
            + os.environ["KUBERNETES_SERVICE_HOST"]
            + ":"
            + os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        )
    identity = "binding-" + digest(payload)[:24]
    db.execute(
        insert(ExecutionBinding)
        .values(id=identity, payload=payload)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    return identity


def binding(db, identity):
    row = db.get(ExecutionBinding, identity or "legacy-simulation-v1")
    if not row:
        raise AppError(409, "Execution binding is missing", "adapter_unavailable")
    return row.payload


def simulated(payload, name):
    if payload.get(name) != SIMULATED:
        raise AppError(
            422, f"{name} adapter is unavailable: {payload.get(name)}", "adapter_unavailable"
        )
    if name == "provider":
        from platform_management.providers.simulated import SimulatedProvider

        return SimulatedProvider()
    if name == "host":
        from platform_management.integrations.ansible import SimulatedHostAutomation

        return SimulatedHostAutomation()
    if name == "cluster":
        from platform_management.integrations.kubernetes import SimulatedClusterAutomation

        return SimulatedClusterAutomation()
    if name == "serving":
        from platform_management.integrations.helm import SimulatedServingAdapter

        return SimulatedServingAdapter()
    raise ValueError("Unknown adapter category")


def runner(payload, run_id, outcome="passing"):
    name = payload.get("runner")
    if name == SIMULATED:
        from platform_management.integrations.runner import SimulatedRunner

        return SimulatedRunner()
    if name in {"kubernetes:v1", "kubernetes-fixture:v1", "kubernetes-workload-stub:v1"}:
        if name == "kubernetes:v1" and (
            payload["mode"] != "real"
            or not is_image_pin(payload.get("runner_image"))
            or not payload.get("runner_command")
        ):
            raise AppError(
                422,
                "A real runner requires a pinned image and execute-only command",
                "adapter_unavailable",
            )
        if (
            name in {"kubernetes-fixture:v1", "kubernetes-workload-stub:v1"}
            and payload["mode"] != "simulation"
        ):
            raise AppError(422, "Fixture runners cannot execute real runs", "adapter_unavailable")
        from platform_management.integrations.job_runner import KubernetesJobRunner

        return KubernetesJobRunner(payload, run_id, outcome)
    raise AppError(422, f"Runner adapter is unavailable: {name}", "adapter_unavailable")


def capabilities():
    c = configuration()
    real = configuration("real")
    return [
        {
            "category": k,
            "simulation": c[k],
            "real": real[k],
            "configured": (
                real[k] == "kubernetes:v1"
                and is_image_pin(real["runner_image"])
                and bool(real["runner_command"])
                and bool(real["artifact_claim"])
            )
            if k == "runner"
            else real[k] != "unconfigured:v1",
        }
        for k in ("provider", "host", "cluster", "serving", "runner")
    ]
