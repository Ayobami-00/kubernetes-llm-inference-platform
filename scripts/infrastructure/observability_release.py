"""Fetch reviewed charts and render monitoring; this entrypoint never deploys."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

import yaml
from common import CONFIG, ROOT, tool
from jsonschema import Draft7Validator

LOCK = ROOT / "deploy/observability.lock.json"
CACHE = ROOT / "artifacts/platform/observability-charts"
PLACEMENT = {"klip.dev.node-restriction.kubernetes.io/platform": "true"}
SOURCES = {
    "kube-prometheus-stack": "prometheus-community/helm-charts",
    "prometheus-blackbox-exporter": "prometheus-community/helm-charts",
    "grafana": "grafana-community/helm-charts",
    "loki": "grafana-community/helm-charts",
    "tempo": "grafana-community/helm-charts",
    "thanos": "thanos-community/helm-charts",
    "opentelemetry-collector": "open-telemetry/opentelemetry-helm-charts",
}


class CRDLoader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
    """Accept Kubernetes' literal '=' enum entry, tagged specially by YAML 1.1."""


CRDLoader.add_constructor(
    "tag:yaml.org,2002:value", lambda loader, node: loader.construct_scalar(node)
)


def fetch() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    lock = json.loads(LOCK.read_text())
    for name, item in lock["charts"].items():
        if (
            name not in SOURCES
            or not item["url"].startswith(f"https://github.com/{SOURCES[name]}/releases/download/")
            or item["file"] != f"{name}-{item['version']}.tgz"
            or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
        ):
            raise ValueError("Unexpected chart identity or source")
        path = CACHE / item["file"]
        if path.is_symlink():
            raise ValueError("Refusing a symlinked archive")
        if not path.exists():
            with urllib.request.urlopen(item["url"], timeout=30) as response:
                body = response.read(16 * 1024 * 1024 + 1)
            if len(body) > 16 * 1024 * 1024:
                raise ValueError("Chart download exceeds its bound")
            if hashlib.sha256(body).hexdigest() != item["sha256"]:
                raise ValueError("Chart download checksum differs")
            path.write_bytes(body)
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("Chart cache checksum differs: " + name)
        # Re-extract the checked archive; a stale template must never survive.
        with tempfile.TemporaryDirectory(dir=CACHE, prefix=".chart-") as temporary:
            with tarfile.open(path) as archive:
                members = archive.getmembers()
                if (
                    len(members) > 10000
                    or sum(member.size for member in members) > 128 * 1024 * 1024
                ):
                    raise ValueError("Extracted chart exceeds its bound")
                if any(
                    not member.name.startswith(name + "/") or member.issym() or member.islnk()
                    for member in members
                ):
                    raise ValueError("Unexpected archive paths/links")
                archive.extractall(temporary, filter="data")
            source = Path(temporary) / name
            chart = yaml.safe_load((source / "Chart.yaml").read_text())
            if chart["name"] != name or chart["version"] != item["version"]:
                raise ValueError("Extracted chart identity differs")
            destination = CACHE / name
            if destination.is_symlink():
                raise ValueError("Refusing a symlinked chart directory")
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(source, destination)
        print("Verified chart", name, item["version"], flush=True)


def image(reference: str) -> str:
    value = json.loads(LOCK.read_text())["images"][reference]
    if value["platform"] != "linux/amd64" or not re.fullmatch(
        r"sha256:[a-f0-9]{64}", value["digest"]
    ):
        raise ValueError("Missing verified Linux AMD64 image")
    return reference + "@" + value["digest"]


def monitoring_values() -> dict:
    values = yaml.safe_load((ROOT / "deploy/observability/monitoring.yaml").read_text())
    operator = values["prometheusOperator"]
    bindings = [
        (operator, "image", "quay.io/prometheus-operator/prometheus-operator:v0.94.2"),
        (
            operator["prometheusConfigReloader"],
            "image",
            "quay.io/prometheus-operator/prometheus-config-reloader:v0.94.2",
        ),
        (operator, "thanosImage", "quay.io/thanos/thanos:v0.42.4"),
        (
            values["prometheus"]["prometheusSpec"],
            "image",
            "quay.io/prometheus/prometheus:v3.15.1-distroless",
        ),
        (
            values["alertmanager"]["alertmanagerSpec"],
            "image",
            "quay.io/prometheus/alertmanager:v0.34.2",
        ),
        (
            values["kube-state-metrics"],
            "image",
            "registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.20.0",
        ),
    ]
    for parent, key, reference in bindings:
        pinned = image(reference)
        name, digest = pinned.split("@")
        repository, tag = name.rsplit(":", 1)
        registry, repository = repository.split("/", 1)
        parent[key] = {
            "registry": registry,
            "repository": repository,
            "tag": tag,
            "sha": digest.removeprefix("sha256:"),
        }
    # This dependency uses @<sha> directly; the parent chart prefixes sha256:.
    values["kube-state-metrics"]["image"]["sha"] = image(
        "registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.20.0"
    ).split("@")[1]
    values["prometheus-node-exporter"]["image"] = {
        "tag": "v1.12.1-distroless",
        "distroless": False,
        "digest": image("quay.io/prometheus/node-exporter:v1.12.1-distroless").split("@")[1],
    }
    prom = values["prometheus"]["prometheusSpec"]
    prom["thanos"]["image"] = image("quay.io/thanos/thanos:v0.42.4")
    prom["thanos"]["objectStorageConfig"] = {
        "secret": {
            "type": "S3",
            "config": {
                "bucket": f"klip-metrics-{CONFIG['account_id']}-{CONFIG['region']}",
                "endpoint": f"s3.{CONFIG['region']}.amazonaws.com",
                "region": CONFIG["region"],
                "aws_sdk_auth": True,
                "insecure": False,
            },
        }
    }
    for name in ("prometheus", "alertmanager"):
        values[name]["serviceAccount"]["annotations"] = {
            "eks.amazonaws.com/role-arn": (
                f"arn:aws:iam::{CONFIG['account_id']}:role/{CONFIG['project']}/"
                f"klip-observability-{name}"
            ),
            "eks.amazonaws.com/sts-regional-endpoints": "true",
            "eks.amazonaws.com/token-expiration": "3600",
        }
    values["alertmanager"]["config"]["receivers"][1]["sns_configs"] = [
        {
            "topic_arn": f"arn:aws:sns:us-east-1:{CONFIG['account_id']}:klip-platform-alerts",
            "sigv4": {"region": "us-east-1"},
            "send_resolved": True,
        }
    ]
    return values


def check_render(documents: list[dict]) -> None:
    """Reject hidden image defaults, non-expiring tokens and accidental host access."""
    pinned = {image(name) for name in json.loads(LOCK.read_text())["images"]}

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "image" and isinstance(item, str) and item not in pinned:
                    raise ValueError("Rendered unpinned image: " + item)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str) and value.startswith(
            ("--prometheus-config-reloader=", "--thanos-default-base-image=")
        ):
            if value.split("=", 1)[1] not in pinned:
                raise ValueError("Operator would launch an unpinned auxiliary container")

    for doc in documents:
        visit(doc)
        if doc.get("type") == "kubernetes.io/service-account-token":
            raise ValueError("Non-expiring Kubernetes API token is forbidden")
        if doc.get("kind") in {"Deployment", "DaemonSet"}:
            pod = doc["spec"]["template"]["spec"]
            if pod.get("hostNetwork") or pod.get("hostPID") or pod.get("hostIPC"):
                raise ValueError("Monitoring workloads cannot bypass Pod networking/isolation")
            if doc["kind"] == "Deployment" and any(
                pod.get("nodeSelector", {}).get(k) != v for k, v in PLACEMENT.items()
            ):
                raise ValueError("Monitoring applications must use protected platform CPU workers")
        if doc.get("kind") in {"Prometheus", "Alertmanager"}:
            spec = doc["spec"]
            if (
                any(spec.get("nodeSelector", {}).get(k) != v for k, v in PLACEMENT.items())
                or not spec.get("affinity", {})
                .get("podAntiAffinity", {})
                .get("requiredDuringSchedulingIgnoredDuringExecution")
                or spec.get("replicas", 1) < 2
            ):
                raise ValueError("Replicated monitoring must be spread on protected CPU hosts")


def render() -> None:
    fetch()
    CACHE.mkdir(parents=True, exist_ok=True)
    values_path = CACHE / "monitoring-values.yaml"
    values_path.write_text(yaml.safe_dump(monitoring_values(), sort_keys=False))
    output = subprocess.run(
        [
            tool("helm"),
            "template",
            "klip-monitoring",
            str(CACHE / "kube-prometheus-stack"),
            "--namespace",
            "observability",
            "--kube-version",
            "1.36.5",
            "-f",
            str(values_path),
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    documents = [doc for doc in yaml.safe_load_all(output) if doc]
    check_render(documents)
    schemas = {}
    for path in (CACHE / "kube-prometheus-stack/charts/crds/crds").glob("*.yaml"):
        crd = yaml.load(path.read_text(), Loader=CRDLoader)
        for version in crd["spec"]["versions"]:
            schemas[(crd["spec"]["names"]["kind"], version["name"])] = version["schema"][
                "openAPIV3Schema"
            ]
    if not schemas:
        raise ValueError("Pinned Prometheus Operator schemas are missing")
    for document in documents:
        if document.get("apiVersion", "").startswith("monitoring.coreos.com/"):
            key = (document["kind"], document["apiVersion"].split("/")[1])
            Draft7Validator(schemas[key]).validate(document)
    (CACHE / "monitoring-rendered.yaml").write_text(output)
    print(f"Rendered and checked {len(documents)} monitoring objects; nothing deployed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["fetch", "render"])
    args = parser.parse_args()
    if args.command == "fetch":
        fetch()
    else:
        render()


if __name__ == "__main__":
    main()
