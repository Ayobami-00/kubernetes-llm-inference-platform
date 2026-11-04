"""Saved-plan update enabling only DCGM discovery and Prometheus egress.

Retain the initial GPU install and its exact values. This transition does not
upgrade images, relax controller egress or label any node as a GPU candidate.
"""

import argparse
import copy
import fcntl
import json
import re
from pathlib import Path

import yaml
from common import source_digest, tool, utc_now, write_private
from gpu_operator import CANDIDATE, NAMESPACE, RELEASE, boundary, folder, render, sha, values
from gpu_operator_security import assess


def validate_delta(previous_values, previous_boundary, previous_rendered):
    expected = copy.deepcopy(values())
    expected["dcgmExporter"]["serviceMonitor"].pop("additionalLabels")
    if yaml.safe_load(previous_values) != expected:
        raise ValueError("GPU telemetry update may only add the monitor discovery label")
    documents = list(yaml.safe_load_all(boundary()))
    if list(yaml.safe_load_all(previous_boundary)) != documents[:-1]:
        raise ValueError("GPU telemetry update may only add Prometheus exporter egress")
    policy = documents[-1]
    if policy["metadata"] != {"name": "prometheus-gpu-exporters", "namespace": "observability"}:
        raise ValueError("Unexpected telemetry boundary")
    old, new = list(yaml.safe_load_all(previous_rendered)), list(yaml.safe_load_all(render()))
    normalized = copy.deepcopy(new)
    for document in normalized:
        if document and document["kind"] == "ClusterPolicy":
            document["spec"]["dcgmExporter"]["serviceMonitor"]["additionalLabels"] = {}
    if old != normalized:
        raise ValueError("GPU telemetry chart delta exceeds the single discovery label")
    return policy


def current(kubectl, active):
    nodes = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    if {n["metadata"]["name"]: n["metadata"]["uid"] for n in nodes} != active["cluster"][
        "nodes"
    ] or any(
        n["metadata"].get("labels", {}).get(next(iter(CANDIDATE)))
        or int(n["status"].get("allocatable", {}).get("nvidia.com/gpu", "0"))
        for n in nodes
    ):
        raise ValueError("Initial telemetry transition requires the unchanged six CPU nodes")
    prometheus = json.loads(kubectl("-n", "observability", "get", "prometheus", "-o", "json"))[
        "items"
    ]
    if (
        len(prometheus) != 1
        or prometheus[0]["spec"].get("serviceMonitorSelector")
        != {"matchLabels": {"release": "klip-monitoring"}}
        or prometheus[0]["spec"].get("serviceMonitorNamespaceSelector") != {}
    ):
        raise ValueError("Prometheus discovery differs from the GPU monitor's explicit labels")
    policy = json.loads(kubectl("get", "clusterpolicy", "cluster-policy", "-o", "json"))
    if policy["spec"]["dcgmExporter"]["serviceMonitor"].get("additionalLabels", {}):
        raise ValueError("GPU monitor labels changed; reconcile the existing update")
    if kubectl(
        "-n",
        "observability",
        "get",
        "ciliumnetworkpolicy",
        "prometheus-gpu-exporters",
        "--ignore-not-found",
        "-o",
        "name",
    ).strip():
        raise ValueError("GPU exporter egress already exists; do not replay the update")
    return {
        "prometheus_uid": prometheus[0]["metadata"]["uid"],
        "policy_uid": policy["metadata"]["uid"],
        "policy_spec_sha256": sha(json.dumps(policy["spec"], sort_keys=True).encode()),
    }


def prepare(security_directory):
    from cluster import kubectl

    active_bytes = (folder() / "active.json").read_bytes()
    active = json.loads(active_bytes)
    if any(u["kind"] == "dcgm-discovery-egress" for u in active.get("updates", [])):
        raise ValueError("GPU telemetry transition was already recorded")
    previous = folder() / "plans" / active["approval_sha256"]
    saved = {
        name: (previous / name).read_text()
        for name in ("values.yaml", "boundary.yaml", "rendered.yaml")
    }
    policy = validate_delta(saved["values.yaml"], saved["boundary.yaml"], saved["rendered.yaml"])
    candidate = render()
    value = {
        "schema_version": "klip-gpu-telemetry-update.v1",
        "created_at": utc_now(),
        "source_sha256": source_digest(),
        "active_sha256": sha(active_bytes),
        "previous": {name: sha(text.encode()) for name, text in saved.items()},
        "values_sha256": sha(yaml.safe_dump(values()).encode()),
        "rendered_sha256": sha(candidate.encode()),
        "policy_sha256": sha(yaml.safe_dump(policy).encode()),
        "live": current(kubectl, active),
        "security_directory": str(security_directory.resolve()),
        "security": assess(security_directory),
    }
    value["approval_sha256"] = sha(json.dumps(value, sort_keys=True).encode())
    root = folder() / "telemetry-updates" / value["approval_sha256"]
    write_private(root / "plan.json", value)
    for name, content in saved.items():
        write_private(root / ("previous-" + name), content)
    write_private(root / "values.yaml", yaml.safe_dump(values()))
    write_private(root / "rendered.yaml", candidate)
    write_private(root / "policy.yaml", yaml.safe_dump(policy))
    return value


def apply(approval):
    from cluster import FOLDER, Host, checked, kubeconfigs, kubectl, load_hosts, tunnel

    if not re.fullmatch(r"[a-f0-9]{64}", approval or ""):
        raise ValueError("A saved GPU telemetry update is required")
    root = folder() / "telemetry-updates" / approval
    plan = json.loads((root / "plan.json").read_text())
    if (
        sha(
            json.dumps(
                {k: v for k, v in plan.items() if k != "approval_sha256"}, sort_keys=True
            ).encode()
        )
        != approval
        or plan["approval_sha256"] != approval
        or plan["source_sha256"] != source_digest()
        or plan["active_sha256"] != sha((folder() / "active.json").read_bytes())
        or plan["values_sha256"] != sha(yaml.safe_dump(values()).encode())
        or plan["values_sha256"] != sha((root / "values.yaml").read_bytes())
        or plan["rendered_sha256"] != sha(render().encode())
        or plan["rendered_sha256"] != sha((root / "rendered.yaml").read_bytes())
        or plan["policy_sha256"] != sha((root / "policy.yaml").read_bytes())
        or plan["security"] != assess(Path(plan["security_directory"]))
        or (root / "result.json").exists()
        or any(
            sha((root / ("previous-" + n)).read_bytes()) != digest
            for n, digest in plan["previous"].items()
        )
    ):
        raise ValueError("GPU telemetry plan changed or was submitted; reconcile its exact result")
    policy = validate_delta(
        *[
            (root / ("previous-" + n)).read_text()
            for n in ("values.yaml", "boundary.yaml", "rendered.yaml")
        ]
    )
    if sha(yaml.safe_dump(policy).encode()) != plan["policy_sha256"]:
        raise ValueError("GPU telemetry boundary differs")
    kubeconfigs()
    result = {"approval_sha256": approval, "status": "running", "started_at": utc_now()}
    with (FOLDER / "operator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        active = json.loads((folder() / "active.json").read_text())
        if current(kubectl, active) != plan["live"]:
            raise ValueError("GPU telemetry live identities or policy changed")
        write_private(root / "previous-active.json", active)
        write_private(root / "result.json", result)
        try:
            Host(load_hosts()["cp-01"]).run(
                ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "create", "-f", "-"],
                data=yaml.safe_dump(policy),
            )
            from gpu_operator import CACHE

            with tunnel():
                output = checked(
                    [
                        tool("helm"),
                        "upgrade",
                        RELEASE,
                        str(CACHE / RELEASE),
                        "--namespace",
                        NAMESPACE,
                        "--kubeconfig",
                        str(FOLDER / "operator.conf"),
                        "--values",
                        str(root / "values.yaml"),
                        "--atomic",
                        "--wait",
                        "--timeout",
                        "5m",
                        "--history-max",
                        "10",
                    ],
                    timeout=360,
                )
            write_private(root / "helm.log", output)
            observed = json.loads(kubectl("get", "clusterpolicy", "cluster-policy", "-o", "json"))
            if observed["metadata"]["uid"] != plan["live"]["policy_uid"] or observed["spec"][
                "dcgmExporter"
            ]["serviceMonitor"]["additionalLabels"] != {"release": "klip-monitoring"}:
                raise ValueError("GPU telemetry policy observation differs after Helm")
            result.update(status="deployed", deployed_at=utc_now(), metrics_ingestion_proven=False)
            active.setdefault("updates", []).append({"kind": "dcgm-discovery-egress", **result})
            write_private(folder() / "active.json", active)
        except Exception as error:
            result.update(status="reconciliation_required", error_type=type(error).__name__)
            raise
        finally:
            write_private(root / "result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "apply"])
    parser.add_argument("--security-directory", type=Path)
    parser.add_argument("--approve-plan")
    args = parser.parse_args()
    if args.command == "prepare" and args.security_directory is None:
        parser.error("--security-directory is required")
    print(
        json.dumps(
            prepare(args.security_directory)
            if args.command == "prepare"
            else apply(args.approve_plan),
            indent=2,
        )
    )
