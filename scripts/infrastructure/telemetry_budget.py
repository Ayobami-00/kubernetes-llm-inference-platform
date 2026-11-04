"""Account native sidecars and preserve declared CPU headroom before a telemetry rollout."""

from __future__ import annotations

import math

from observability_release import PLACEMENT


def quantity(value, resource):
    value = str(value)
    factors = (
        {"m": 0.001, "u": 0.000001, "n": 0.000000001}
        if resource == "cpu"
        else {
            "Ki": 1024,
            "Mi": 1024**2,
            "Gi": 1024**3,
            "Ti": 1024**4,
            "k": 1000,
            "M": 1000**2,
            "G": 1000**3,
        }
    )
    for suffix, scale in factors.items():
        if value.endswith(suffix):
            result = float(value[: -len(suffix)]) * scale
            break
    else:
        result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("Invalid resource quantity")
    return result


def requests(pod):
    def read(container):
        raw = container.get("resources", {}).get("requests", {})
        return [quantity(raw.get(k, 0), k) for k in ("cpu", "memory")]

    app = [sum(read(c)[i] for c in pod.get("containers", [])) for i in (0, 1)]
    sidecars, peak = [0.0, 0.0], [0.0, 0.0]
    for container in pod.get("initContainers", []):
        resource = read(container)
        if container.get("restartPolicy") == "Always":
            sidecars = [sidecars[i] + resource[i] for i in (0, 1)]
            resource = sidecars
        else:
            resource = [resource[i] + sidecars[i] for i in (0, 1)]
        peak = [max(peak[i], resource[i]) for i in (0, 1)]
    return [
        max(app[i] + sidecars[i], peak[i]) + quantity(pod.get("overhead", {}).get(k, 0), k)
        for i, k in enumerate(("cpu", "memory"))
    ]


def verify(documents, component):
    import json

    from cluster import kubectl

    pods = json.loads(kubectl("get", "pods", "-A", "-o", "json"))["items"]
    current = [0.0, 0.0]
    for pod in pods:
        if pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
            continue
        spec = pod["spec"]
        if not (
            spec.get("nodeName") in {"platform-01", "platform-02", "platform-03"}
            or all(spec.get("nodeSelector", {}).get(k) == v for k, v in PLACEMENT.items())
        ):
            continue
        if (
            pod["metadata"].get("labels", {}).get("app.kubernetes.io/instance")
            == "klip-" + component
        ):
            continue
        resource = requests(spec)
        current = [current[i] + resource[i] for i in (0, 1)]
    desired = [0.0, 0.0]
    for doc in documents:
        if doc["kind"] not in {"Deployment", "StatefulSet"}:
            continue
        resource = requests(doc["spec"]["template"]["spec"])
        replicas = doc["spec"].get("replicas", 1)
        desired = [desired[i] + resource[i] * replicas for i in (0, 1)]
    # Hold space for one additional 1-CPU/2-GiB runner, not unlimited admissions.
    projected = [current[0] + desired[0] + 1, current[1] + desired[1] + 2 * 1024**3]
    if projected[0] > 13.5 or projected[1] > 52 * 1024**3:
        raise ValueError("Telemetry and runner requests exceed the reviewed one-worker-loss budget")
    return {
        "current_cpu": current[0],
        "desired_cpu": desired[0],
        "projected_cpu_including_one_runner": projected[0],
        "projected_memory_gib_including_one_runner": projected[1] / 1024**3,
        "single_worker_loss_capacity": {"cpu": 15, "memory_gib": 58},
        "limitations": (
            "Request accounting with 10% margin; not a scheduler, performance or unplanned-failure "
            "proof. Additional runner admissions require separate budgeting."
        ),
    }
