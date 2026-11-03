"""Immutable plans for private telemetry components following core monitoring."""

from __future__ import annotations

import argparse
import fcntl
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from common import private_dir, source_digest, state_home, tool, utc_now, write_private
from monitoring_runtime import admission_fail_closed, sha, verify_boundary
from observability import verify_roles
from observability_release import BACKENDS, CACHE, backend_values, render, thanos_values
from observability_security import assess
from platform_resize import verify_capacity

COMPONENT = "thanos"
TOPOLOGIES = {
    "thanos": [
        ("deployment", "klip-thanos-query", 2),
        ("statefulset", "klip-thanos-storegateway", 2),
        ("statefulset", "klip-thanos-compactor", 1),
    ],
    "grafana": [("deployment", "klip-grafana", 2)],
    "loki": [("statefulset", "klip-loki", 3)],
    "tempo": [("statefulset", "klip-tempo", 1)],
    "otel": [("statefulset", "klip-otel", 2)],
}


def component_values():
    return thanos_values() if COMPONENT == "thanos" else backend_values(COMPONENT)


def review(directory):
    if COMPONENT == "thanos":
        return assess(directory)
    from telemetry_security import assess_component

    return assess_component(directory, COMPONENT)


def folder():
    return private_dir(state_home() / "cluster/observability" / COMPONENT)


def archive_current():
    active = folder() / "active.json"
    if not active.exists():
        return
    old = json.loads(active.read_text())
    destination = folder() / "history" / old["approval_sha256"]
    manifest = destination / "manifest.json"
    if manifest.exists():
        for name, checksum in json.loads(manifest.read_text()).items():
            if sha((destination / name).read_text()) != checksum:
                raise ValueError("Archived telemetry evidence changed")
        return
    for name in ("active.json", "values.yaml", "helm.log", "observation.json"):
        source = folder() / name
        target = destination / name
        if source.exists():
            if target.exists():
                if target.read_bytes() != source.read_bytes():
                    raise ValueError("Original telemetry history changed before archival")
            else:
                private_dir(destination)
                shutil.copyfile(source, target)
                target.chmod(0o600)

    write_private(
        manifest, {p.name: sha(p.read_text()) for p in destination.iterdir() if p.is_file()}
    )


def reconcile(approval):
    """Retire only an observed, fully rolled-back first install; never replay it."""
    from cluster import kubectl

    current = json.loads((folder() / "active.json").read_text())
    if current["approval_sha256"] != approval:
        raise ValueError("Select the original interrupted telemetry operation")
    if current["status"] == "failed":
        return current
    if current["status"] != "interrupted" or current.get("interrupted_stage") != "installing":
        raise ValueError("Only a separately observed atomic install rollback can be retired")
    secrets = json.loads(
        kubectl(
            "-n",
            "observability",
            "get",
            "secret",
            "-l",
            "owner=helm,name=klip-" + COMPONENT,
            "-o",
            "json",
        )
    )["items"]
    workloads = json.loads(
        kubectl(
            "-n",
            "observability",
            "get",
            "deployments,statefulsets,pods",
            "-l",
            "app.kubernetes.io/instance=klip-" + COMPONENT,
            "-o",
            "json",
        )
    )["items"]
    if secrets or workloads:
        raise ValueError(
            "Helm release or workloads remain; preserve the ambiguous original attempt"
        )
    claims = retained_claims()
    current.update(
        status="failed", reconciled_at=utc_now(), rollback_observed=True, retained_claims=claims
    )
    write_private(folder() / (approval + ".result.json"), current)
    write_private(folder() / "active.json", current)
    return current


def retained_claims():
    from cluster import kubectl
    from monitoring_runtime import get

    claims = json.loads(
        kubectl(
            "-n",
            "observability",
            "get",
            "pvc",
            "-l",
            "app.kubernetes.io/instance=klip-" + COMPONENT,
            "-o",
            "json",
        )
    )["items"]
    documents = [
        d for d in yaml.safe_load_all((CACHE / (COMPONENT + "-rendered.yaml")).read_text()) if d
    ]
    expected = {}
    names = set()
    for doc in documents:
        if doc.get("kind") != "StatefulSet":
            continue
        names.add(doc["spec"]["selector"]["matchLabels"]["app.kubernetes.io/name"])
        for claim in doc["spec"].get("volumeClaimTemplates", []):
            for index in range(doc["spec"].get("replicas", 1)):
                name = f"{claim['metadata']['name']}-{doc['metadata']['name']}-{index}"
                expected[name] = claim["spec"]["resources"]["requests"]["storage"]
    if any(claim["metadata"]["name"] not in expected for claim in claims):
        raise ValueError("Unexpected retained Thanos claim")
    result = {}
    for name in expected:
        claim = get("pvc", name, "observability")
        if not claim:
            continue
        spec = claim["spec"]
        labels = claim["metadata"].get("labels", {})
        if (
            labels.get("app.kubernetes.io/instance") != "klip-" + COMPONENT
            or labels.get("app.kubernetes.io/name") not in names
            or spec.get("storageClassName") != "klip-gp3"
            or spec.get("accessModes") != ["ReadWriteOnce"]
            or spec["resources"]["requests"]["storage"] != expected[name]
        ):
            raise ValueError("Unexpected retained Thanos volume; no implicit adoption/deletion")
        result[name] = {"uid": claim["metadata"]["uid"], "spec_sha256": sha(spec)}
    return result


def prepare(directory):
    active = folder() / "active.json"
    if active.exists() and json.loads(active.read_text())["status"] not in {"installed", "failed"}:
        raise ValueError("Reconcile the original telemetry deployment before preparing another")
    if not admission_fail_closed():
        raise ValueError("Core monitoring admission must already verify TLS and fail closed")
    verify_boundary()
    capacity = verify_capacity()
    identities = verify_roles()
    render(COMPONENT)
    security = review(directory)
    claims = retained_claims()
    from telemetry_budget import verify as verify_budget

    budget = verify_budget(
        [d for d in yaml.safe_load_all((CACHE / (COMPONENT + "-rendered.yaml")).read_text()) if d],
        COMPONENT,
    )
    plan = {
        "schema_version": "klip-telemetry-component-plan.v1",
        "component": COMPONENT,
        "release": "klip-" + COMPONENT,
        "namespace": "observability",
        "prepared_at": utc_now(),
        "source_sha256": source_digest(),
        "security_directory": str(directory.resolve()),
        "security": security,
        "request_budget": budget,
        "retained_claims": claims,
        "values": component_values(),
        "roles_verified_at": identities["verified_at"],
        "node_uids": {name: item["uid"] for name, item in capacity["nodes"].items()},
        "storage_budget": {
            "gp3_gib": {"thanos": 20, "grafana": 0, "loki": 30, "tempo": 10, "otel": 10}[COMPONENT]
        },
        "workloads": TOPOLOGIES[COMPONENT],
    }
    if COMPONENT == "grafana":
        from grafana_storage import credentials

        plan["runtime_credentials_sha256"] = sha(credentials())
    plan["approval_sha256"] = sha(plan)
    archive_current()
    write_private(folder() / "plan.json", plan)
    write_private(folder() / (plan["approval_sha256"] + ".json"), plan)
    return plan


def observe():
    from monitoring_runtime import get

    report = {"observed_at": utc_now(), "ready": True, "workloads": {}}
    for kind, component, replicas in TOPOLOGIES[COMPONENT]:
        item = get(kind, component, "observability")
        status = item.get("status", {}) if item else {}
        ok = bool(
            item
            and item["spec"].get("replicas") == replicas
            and status.get("observedGeneration") == item["metadata"]["generation"]
            and status.get("readyReplicas", 0) == replicas
            and status.get("updatedReplicas", 0) == replicas
        )
        report["workloads"][component] = {"ready": ok, "replicas": status.get("readyReplicas", 0)}
        report["ready"] &= ok
    write_private(folder() / "observation.json", report)
    return report


def apply(approval):
    from cluster import FOLDER, Host, checked, kubeconfigs, load_hosts, tunnel
    from smoke import wait_until

    plan = json.loads((folder() / "plan.json").read_text())
    if (
        approval != plan["approval_sha256"]
        or sha({k: v for k, v in plan.items() if k != "approval_sha256"}) != approval
    ):
        raise ValueError("Select the exact prepared telemetry plan")
    active_path = folder() / "active.json"
    if active_path.exists():
        active = json.loads(active_path.read_text())
        if active["approval_sha256"] == approval:
            if active["status"] == "installed":
                return active
            raise ValueError("Observe the original interrupted Helm operation; no automatic replay")
        if active["status"] not in {"installed", "failed"}:
            raise ValueError("A different interrupted telemetry operation remains unresolved")
    if (
        plan["source_sha256"] != source_digest()
        or plan["values"] != component_values()
        or not timedelta(0)
        <= datetime.now(UTC) - datetime.fromisoformat(plan["prepared_at"])
        <= timedelta(hours=1)
    ):
        raise ValueError("Telemetry source changed or plan expired")
    verify_boundary()
    capacity = verify_capacity()
    if plan["node_uids"] != {name: item["uid"] for name, item in capacity["nodes"].items()}:
        raise ValueError("Platform identity changed after telemetry planning")
    verify_roles()
    review(Path(plan["security_directory"]))
    render(COMPONENT)
    if plan.get("retained_claims", {}) != retained_claims():
        raise ValueError("Retained Thanos volume identity changed after planning")
    active = {"approval_sha256": approval, "status": "validating", "started_at": utc_now()}
    write_private(active_path, active)
    try:
        if COMPONENT == "grafana":
            from grafana_storage import credentials, initialize

            if plan["runtime_credentials_sha256"] != sha(credentials()):
                raise ValueError("Grafana credential custody changed after planning")
            active["database"] = initialize()
            from telemetry_secrets import stage_local

            active["secret_custody"] = stage_local()

        from observability_release import validate_pod_admission

        documents = [
            d for d in yaml.safe_load_all((CACHE / (COMPONENT + "-rendered.yaml")).read_text()) if d
        ]
        from telemetry_budget import verify as verify_budget

        active["request_budget"] = verify_budget(documents, COMPONENT)
        active["pod_specs_admitted"] = validate_pod_admission(documents)
        write_private(active_path, active)
        Host(load_hosts()["cp-01"]).run(
            [
                "sudo",
                "kubectl",
                "--kubeconfig=/etc/kubernetes/admin.conf",
                "apply",
                "--dry-run=server",
                "--field-manager=klip-telemetry-review",
                "-f",
                "-",
            ],
            data=(CACHE / (COMPONENT + "-rendered.yaml")).read_text(),
        )
        values = folder() / "values.yaml"
        write_private(values, yaml.safe_dump(plan["values"]))
        active.update(status="installing", helm_started_at=utc_now())
        write_private(active_path, active)
        kubeconfigs()
        with tunnel():
            log = checked(
                [
                    tool("helm"),
                    "upgrade",
                    "--install",
                    "klip-" + COMPONENT,
                    str(CACHE / ("opentelemetry-collector" if COMPONENT == "otel" else COMPONENT)),
                    "--namespace",
                    "observability",
                    "--kubeconfig",
                    str(FOLDER / "operator.conf"),
                    "--values",
                    str(values),
                    "--atomic",
                    "--wait",
                    "--timeout",
                    "10m",
                    "--history-max",
                    "10",
                ],
                timeout=750,
            )
        write_private(folder() / "helm.log", log)
        observed = wait_until(
            lambda: r if (r := observe())["ready"] else None,
            "replicated Thanos and singleton compactor",
            timeout=300,
            interval=5,
        )
        active.update(status="installed", completed_at=utc_now(), observation=observed)
        write_private(active_path, active)
        write_private(folder() / (approval + ".result.json"), active)
        return active
    except Exception as error:
        active.update(
            interrupted_stage=active["status"],
            status="interrupted",
            interrupted_at=utc_now(),
            error_type=type(error).__name__,
        )
        write_private(active_path, active)
        raise


def main():
    global COMPONENT

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "apply", "observe", "reconcile"])
    parser.add_argument("--component", choices=["thanos", *BACKENDS], default="thanos")
    parser.add_argument("--security-directory", type=Path)
    parser.add_argument("--approve-plan")
    args = parser.parse_args()
    COMPONENT = args.component
    with (private_dir(state_home() / "cluster") / "operator.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "prepare":
            if not args.security_directory:
                parser.error("--security-directory is required")
            result = prepare(args.security_directory)
        elif args.command == "reconcile":
            result = reconcile(args.approve_plan)
        elif args.command == "apply":
            result = apply(args.approve_plan)
        else:
            result = observe()
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
