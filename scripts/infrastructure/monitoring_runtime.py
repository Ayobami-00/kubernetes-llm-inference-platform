"""Plan and install the locked private monitoring release with durable evidence."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from common import (
    PROFILES,
    Aws,
    private_dir,
    source_digest,
    state_home,
    tool,
    utc_now,
    write_private,
)
from observability_release import CACHE, CRDLoader, monitoring_values, render
from observability_security import assess

RELEASE = "klip-monitoring"
NAMESPACE = "observability"


def folder():
    return private_dir(state_home() / "cluster/observability/monitoring")


def sha(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def get(kind, name, namespace=None):
    from cluster import kubectl

    args = ["-n", namespace] if namespace else []
    data = kubectl(*args, "get", kind, name, "--ignore-not-found", "-o", "json").strip()
    return json.loads(data) if data else None


def verify_boundary():
    from cluster import kubectl
    from platform_services import render as render_service
    from platform_services import values_for

    documents = [d for d in yaml.safe_load_all(render_service("klip-observability", {})) if d]
    for desired in documents:
        kind, metadata = desired["kind"], desired["metadata"]
        actual = get(kind, metadata["name"], metadata.get("namespace"))
        if not actual:
            raise ValueError("Missing private monitoring boundary: " + metadata["name"])
        if kind == "Namespace":
            if any(
                actual["metadata"].get("labels", {}).get(k) != v
                for k, v in metadata["labels"].items()
            ):
                raise ValueError("Telemetry namespace security labels differ")
        elif actual["spec"] != desired["spec"]:
            raise ValueError("Live telemetry network policy differs from the reviewed boundary")
        if (
            actual["metadata"].get("annotations", {}).get("meta.helm.sh/release-name")
            != "klip-observability"
        ):
            raise ValueError("Telemetry boundary ownership differs")
    for namespace in (NAMESPACE, "observability-node-agents"):
        for kind in ("NetworkPolicy", "CiliumNetworkPolicy"):
            observed = json.loads(kubectl("-n", namespace, "get", kind, "-o", "json"))["items"]
            expected = {
                d["metadata"]["name"]
                for d in documents
                if d["kind"] == kind and d["metadata"].get("namespace") == namespace
            }
            if {d["metadata"]["name"] for d in observed} != expected:
                raise ValueError("Unexpected additive policy in telemetry namespace")
    hooks = get("MutatingWebhookConfiguration", "klip-workload-identity")
    expected_hook = next(
        d
        for d in yaml.safe_load_all(
            render_service(
                "klip-workload-identity",
                values_for("klip-workload-identity"),
            )
        )
        if d and d["kind"] == "MutatingWebhookConfiguration"
    )
    if (
        not hooks
        or hooks["webhooks"][0]["matchConditions"]
        != expected_hook["webhooks"][0]["matchConditions"]
        or hooks["webhooks"][0]["failurePolicy"] != "Fail"
    ):
        raise ValueError("Exact-subject Pod federation admission is not active")
    return {"verified_at": utc_now(), "resources": len(documents)}


def crds():
    result = []
    for path in sorted((CACHE / "kube-prometheus-stack/charts/crds/crds").glob("*.yaml")):
        doc = yaml.load(path.read_text(), Loader=CRDLoader)
        if (
            doc["kind"] != "CustomResourceDefinition"
            or doc["spec"]["group"] != "monitoring.coreos.com"
        ):
            raise ValueError("Unexpected monitoring CRD identity")
        doc["metadata"].setdefault("labels", {})["klip.dev/managed-by"] = "klip-monitoring-crds"
        doc["metadata"].setdefault("annotations", {})["klip.dev/spec-sha256"] = sha(doc["spec"])
        result.append(doc)
    if len(result) != 10:
        raise ValueError("Expected all ten CRDs from the reviewed operator archive")
    return result


def check_crd(existing, desired):
    if not existing:
        return
    spec = dict(existing["spec"])
    # API-server defaults absent in the upstream document are not schema drift.
    for key, default in (("conversion", {"strategy": "None"}), ("preserveUnknownFields", False)):
        if key not in desired["spec"] and spec.get(key) == default:
            spec.pop(key)
    if (
        existing["metadata"].get("labels", {}).get("klip.dev/managed-by") != "klip-monitoring-crds"
        or spec != desired["spec"]
    ):
        raise ValueError("Existing monitoring CRD ownership/schema differs; no forced adoption")


def prepare(security_directory):
    from observability import verify_roles
    from platform_resize import verify_capacity

    active_path = folder() / "active.json"
    if active_path.exists() and json.loads(active_path.read_text())["status"] not in {
        "installed",
        "failed",
    }:
        raise ValueError("Reconcile the original interrupted monitoring operation first")
    Aws(PROFILES["foundation"]).preflight("foundation")
    capacity = verify_capacity()
    boundary = verify_boundary()
    render()
    assessment = assess(security_directory)
    identities = verify_roles()
    definitions = crds()
    for doc in definitions:
        check_crd(get("crd", doc["metadata"]["name"]), doc)
    plan = {
        "schema_version": "klip-monitoring-plan.v1",
        "prepared_at": utc_now(),
        "source_sha256": source_digest(),
        "security_directory": str(security_directory.resolve()),
        "security": assessment,
        "identities": identities,
        "boundary": boundary,
        "capacity_verified_at": capacity["verified_at"],
        "storage_budget": {
            "gp3_gib": 43,
            "estimated_monthly_usd": 3.99,
            "pricing_review": "2026-11-03 AWS Pricing GetProducts, eu-west-2 gp3, $0.0928/GB-month",
            "limitations": "Retained volumes, tax, snapshots and transfer may add charges",
        },
        "node_uids": {k: v["uid"] for k, v in capacity["nodes"].items()},
        "values_sha256": sha(monitoring_values()),
        "crds_sha256": sha(definitions),
        "status": "prepared",
    }
    plan["approval_sha256"] = sha(plan)
    write_private(folder() / "plan.json", plan)
    write_private(folder() / (plan["approval_sha256"] + ".json"), plan)
    return plan


def observe():
    from cluster import kubectl

    result = {"observed_at": utc_now(), "workloads": {}, "ready": True}
    for kind, name, namespace, replicas in (
        ("deployment", "klip-monitoring-operator", NAMESPACE, 1),
        ("deployment", "klip-monitoring-kube-state-metrics", NAMESPACE, 1),
        ("statefulset", "prometheus-klip-monitoring-prometheus", NAMESPACE, 2),
        ("statefulset", "alertmanager-klip-monitoring-alertmanager", NAMESPACE, 3),
    ):
        doc = get(kind, name, namespace)
        status = doc.get("status", {}) if doc else {}
        ok = bool(
            doc
            and doc["spec"].get("replicas") == replicas
            and status.get("observedGeneration") == doc["metadata"]["generation"]
            and status.get("readyReplicas", 0) == replicas
            and status.get("updatedReplicas", 0) == replicas
        )
        result["workloads"][name] = {"ready": ok, "ready_replicas": status.get("readyReplicas", 0)}
        result["ready"] &= ok
    agent = get(
        "daemonset", "klip-monitoring-prometheus-node-exporter", "observability-node-agents"
    )
    status = agent.get("status", {}) if agent else {}
    nodes = json.loads(kubectl("get", "nodes", "-l", "kubernetes.io/os=linux", "-o", "json"))[
        "items"
    ]
    ok = bool(
        agent
        and status.get("observedGeneration") == agent["metadata"]["generation"]
        and status.get("desiredNumberScheduled") == len(nodes)
        and status.get("numberReady") == len(nodes)
        and status.get("updatedNumberScheduled") == len(nodes)
    )
    result["workloads"]["node-exporter"] = {
        "ready": ok,
        "ready_replicas": status.get("numberReady", 0),
    }
    result["ready"] &= ok
    write_private(folder() / "observation.json", result)
    return result


def apply(approval):
    from cluster import FOLDER, Host, checked, kubeconfigs, kubectl, load_hosts, tunnel
    from observability import verify_roles
    from platform_resize import verify_capacity
    from smoke import wait_until

    plan = json.loads((folder() / "plan.json").read_text())
    original = {k: v for k, v in plan.items() if k != "approval_sha256"}
    if approval != plan["approval_sha256"] or sha(original) != approval:
        raise ValueError("Select the exact reviewed monitoring plan")
    active_path = folder() / "active.json"
    if active_path.exists():
        previous = json.loads(active_path.read_text())
        if previous["approval_sha256"] == approval:
            if previous["status"] == "installed":
                return previous
            raise ValueError("Inspect the original interrupted Helm operation; no automatic replay")
    if (
        plan["source_sha256"] != source_digest()
        or not timedelta(0)
        <= datetime.now(UTC) - datetime.fromisoformat(plan["prepared_at"])
        <= timedelta(hours=1)
        or plan["values_sha256"] != sha(monitoring_values())
    ):
        raise ValueError("Monitoring source/configuration changed or plan expired")
    render()  # Re-extract checksum-verified charts; never trust a stale cache.
    assess(Path(plan["security_directory"]))
    verify_roles()
    capacity = verify_capacity()
    if plan["node_uids"] != {k: v["uid"] for k, v in capacity["nodes"].items()}:
        raise ValueError("Platform identity changed after monitoring review")
    verify_boundary()
    definitions = crds()
    if sha(definitions) != plan["crds_sha256"]:
        raise ValueError("Monitoring schemas changed")
    active = {"approval_sha256": approval, "status": "installing-crds", "started_at": utc_now()}
    write_private(active_path, active)
    host = Host(load_hosts()["cp-01"])
    prefix = ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf"]
    try:
        for doc in definitions:
            existing = get("crd", doc["metadata"]["name"])
            check_crd(existing, doc)
            if not existing:
                host.run([*prefix, "create", "-f", "-"], data=yaml.safe_dump(doc))
        kubectl(
            "wait",
            "--for=condition=Established",
            "--timeout=60s",
            "crd",
            *[d["metadata"]["name"] for d in definitions],
        )
        rendered = (CACHE / "monitoring-rendered.yaml").read_text()
        host.run(
            [
                *prefix,
                "apply",
                "--server-side",
                "--dry-run=server",
                "--field-manager=klip-monitoring-review",
                "-f",
                "-",
            ],
            data=rendered,
        )
        values_path = folder() / "values.yaml"
        write_private(values_path, yaml.safe_dump(monitoring_values()))
        active.update(status="installing", schemas_established_at=utc_now())
        write_private(active_path, active)
        kubeconfigs()
        with tunnel():
            command = [
                tool("helm"),
                "upgrade",
                "--install",
                RELEASE,
                str(CACHE / "kube-prometheus-stack"),
                "--namespace",
                NAMESPACE,
                "--kubeconfig",
                str(FOLDER / "operator.conf"),
                "--values",
                str(values_path),
                "--skip-crds",
                "--atomic",
                "--wait",
                "--timeout",
                "10m",
                "--history-max",
                "10",
            ]
            output = checked(command, timeout=750)
        write_private(folder() / "helm.log", output)
        observed = wait_until(
            lambda: r if (r := observe())["ready"] else None,
            "replicated monitoring workloads",
            timeout=300,
            interval=5,
        )
        active.update(status="installed", completed_at=utc_now(), observation=observed)
        write_private(active_path, active)
        return active
    except Exception as error:
        active.update(
            status="interrupted", interrupted_at=utc_now(), error_type=type(error).__name__
        )
        write_private(active_path, active)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "apply", "observe"])
    parser.add_argument("--security-directory", type=Path)
    parser.add_argument("--approve-plan")
    args = parser.parse_args()
    with (private_dir(state_home() / "cluster") / "operator.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "prepare":
            if not args.security_directory:
                parser.error("--security-directory is required")
            result = prepare(args.security_directory)
        elif args.command == "apply":
            result = apply(args.approve_plan)
        else:
            result = observe()
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
