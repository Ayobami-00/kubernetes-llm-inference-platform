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
    lock = json.loads(LOCK.read_text())
    value = {**lock["images"], **lock.get("telemetry_images", {})}[reference]
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
        (operator, "thanosImage", "quay.io/thanos/thanos:v0.42.5"),
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
    prom["thanos"]["image"] = image("quay.io/thanos/thanos:v0.42.5")
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


def thanos_values() -> dict:
    values = yaml.safe_load((ROOT / "deploy/observability/thanos.yaml").read_text())
    values["global"]["image"] = {
        "registry": "quay.io",
        "repository": "thanos/thanos",
        "tag": image("quay.io/thanos/thanos:v0.42.5").split(":", 1)[1],
    }
    values["global"]["objstore"] = {
        "createSecret": True,
        "secretName": "klip-thanos-objstore",
        "secretKey": "objstore.yml",
        "config": yaml.safe_dump(
            {
                "type": "S3",
                "config": {
                    "bucket": f"klip-metrics-{CONFIG['account_id']}-{CONFIG['region']}",
                    "endpoint": f"s3.{CONFIG['region']}.amazonaws.com",
                    "region": CONFIG["region"],
                    "aws_sdk_auth": True,
                    "insecure": False,
                },
            }
        ),
    }
    for component, role in (("storegateway", "thanos-store"), ("compactor", "thanos-compactor")):
        values[component]["serviceAccount"]["annotations"] = {
            "eks.amazonaws.com/role-arn": (
                f"arn:aws:iam::{CONFIG['account_id']}:role/{CONFIG['project']}/klip-observability-{role}"
            ),
            "eks.amazonaws.com/sts-regional-endpoints": "true",
            "eks.amazonaws.com/token-expiration": "3600",
        }
    for component in ("query", "storegateway"):
        selector = {
            "app.kubernetes.io/name": "thanos",
            "app.kubernetes.io/instance": "klip-thanos",
            "app.kubernetes.io/component": component,
        }
        values[component]["affinity"] = {
            "podAntiAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "topologyKey": "kubernetes.io/hostname",
                        "labelSelector": {"matchLabels": selector},
                    }
                ]
            }
        }
    return values


BACKENDS = {
    "grafana": "docker.io/grafana/grafana:13.3.0-distroless",
    "loki": "docker.io/grafana/loki:3.7.9",
    "tempo": "docker.io/grafana/tempo:3.1.1",
    "otel": "docker.io/otel/opentelemetry-collector-contrib:0.163.2",
}


def backend_values(component):
    if component not in BACKENDS:
        raise ValueError("Unreviewed telemetry component")
    values = yaml.safe_load((ROOT / "deploy/observability" / (component + ".yaml")).read_text())
    pinned = image(BACKENDS[component])
    reference, digest = pinned.split("@")
    repository, tag = reference.rsplit(":", 1)
    registry, repository = repository.split("/", 1)
    if component == "grafana":
        directory = ROOT / "observability/grafana/dashboards"
        dashboards = {
            p.name: json.dumps(json.loads(p.read_text()), separators=(",", ":"))
            for p in sorted(directory.glob("*.json"))
        }
        if len(dashboards) != 4 or sum(len(v.encode()) for v in dashboards.values()) > 750000:
            raise ValueError("Unexpected provisioned dashboard set or ConfigMap size")
        # extraObjects is evaluated by Helm tpl. A quoted Go string emits
        # each JSON document literally, preserving Grafana {{label}} templates.
        manifest = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: klip-grafana-dashboards\ndata:\n"
        )
        for filename, content in dashboards.items():
            manifest += "  " + filename + ": |-\n    {{ " + json.dumps(content) + " }}\n"
        values["extraObjects"] = [manifest]
        values["dashboardsConfigMaps"] = {"klip": "klip-grafana-dashboards"}
        values["dashboardProviders"] = {
            "dashboardproviders.yaml": {
                "apiVersion": 1,
                "providers": [
                    {
                        "name": "klip",
                        "orgId": 1,
                        "folder": "KLIP",
                        "type": "file",
                        "disableDeletion": True,
                        "allowUiUpdates": False,
                        "updateIntervalSeconds": 30,
                        "options": {"path": "/var/lib/grafana/dashboards/klip"},
                    }
                ],
            }
        }
        values["image"] = {
            "registry": registry,
            "repository": repository,
            "tag": tag,
            "sha": digest.removeprefix("sha256:"),
        }
    elif component == "loki":
        values["loki"]["image"] = {
            "registry": registry,
            "repository": repository,
            "tag": tag,
            "digest": digest,
        }
    elif component == "tempo":
        values["tempo"].update(registry=registry, repository=repository, tag=tag + "@" + digest)
    else:
        values["image"] = {"repository": registry + "/" + repository, "tag": tag, "digest": digest}
    if component in {"loki", "tempo"}:
        values["serviceAccount"]["annotations"] = {
            "eks.amazonaws.com/role-arn": (
                f"arn:aws:iam::{CONFIG['account_id']}:role/{CONFIG['project']}/"
                f"klip-observability-{component}"
            ),
            "eks.amazonaws.com/sts-regional-endpoints": "true",
            "eks.amazonaws.com/token-expiration": "3600",
        }
    return values


def check_pod_security(pod: dict) -> None:
    """Controllers are not Pods: reject template omissions before Helm waits."""
    context = pod.get("securityContext", {})
    containers = pod.get("initContainers", []) + pod.get("containers", [])
    for container in containers:
        security = container.get("securityContext", {})
        if security.get("seccompProfile", context.get("seccompProfile", {})).get("type") != (
            "RuntimeDefault"
        ):
            raise ValueError("Every telemetry container requires RuntimeDefault seccomp")
        if (
            not security.get("runAsNonRoot", context.get("runAsNonRoot", False))
            or security.get("runAsUser", context.get("runAsUser")) == 0
            or security.get("privileged", False)
            or security.get("allowPrivilegeEscalation") is not False
            or "ALL" not in security.get("capabilities", {}).get("drop", [])
            or security.get("capabilities", {}).get("add")
            or security.get("readOnlyRootFilesystem") is not True
            or any(port.get("hostPort") for port in container.get("ports", []))
        ):
            raise ValueError("Telemetry container security boundary is incomplete")


def validate_pod_admission(documents: list[dict]) -> list[str]:
    """Validate Pod specs/PSS without creating workloads or assuming future SAs exist."""
    import copy

    from cluster import Host, load_hosts

    host = Host(load_hosts()["cp-01"])
    admitted = []
    for document in documents:
        if document.get("kind") not in {"Deployment", "StatefulSet"}:
            continue
        pod = {"apiVersion": "v1", "kind": "Pod", **copy.deepcopy(document["spec"]["template"])}
        pod["metadata"].update(
            name=document["metadata"]["name"] + "-admission-canary", namespace="observability"
        )
        # SAs may not exist until Helm creates them. This preflight validates
        # PSS/spec only; actual exact-subject federation is verified after rollout.
        pod["spec"]["serviceAccountName"] = "default"
        pod["spec"]["automountServiceAccountToken"] = False
        for claim in document["spec"].get("volumeClaimTemplates", []):
            pod["spec"].setdefault("volumes", []).append(
                {
                    "name": claim["metadata"]["name"],
                    "persistentVolumeClaim": {
                        "claimName": claim["metadata"]["name"]
                        + "-"
                        + document["metadata"]["name"]
                        + "-0"
                    },
                }
            )
        host.run(
            [
                "sudo",
                "kubectl",
                "--kubeconfig=/etc/kubernetes/admin.conf",
                "create",
                "--dry-run=server",
                "-f",
                "-",
                "-o",
                "name",
            ],
            data=json.dumps(pod),
        )
        admitted.append(document["metadata"]["name"])
    return admitted


def check_render(documents: list[dict]) -> None:
    """Reject hidden image defaults, non-expiring tokens and accidental host access."""
    lock = json.loads(LOCK.read_text())
    pinned = {image(name) for name in {**lock["images"], **lock.get("telemetry_images", {})}}
    pinned |= {ref.split("@")[0].rsplit(":", 1)[0] + "@" + ref.split("@")[1] for ref in pinned}

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
        if (
            doc.get("kind") in {"Role", "ClusterRole"}
            and doc.get("metadata", {}).get("name") == "klip-monitoring-kube-state-metrics"
            and any(
                set(rule.get("resources", [])) & {"secrets", "configmaps", "*"}
                for rule in doc.get("rules", [])
            )
        ):
            raise ValueError("Object metrics must not read Secrets or ConfigMaps")
        if doc.get("type") == "kubernetes.io/service-account-token":
            raise ValueError("Non-expiring Kubernetes API token is forbidden")
        if doc.get("kind") in {"Deployment", "DaemonSet", "StatefulSet"}:
            pod = doc["spec"]["template"]["spec"]
            check_pod_security(pod)
            if pod.get("hostNetwork") or pod.get("hostPID") or pod.get("hostIPC"):
                raise ValueError("Monitoring workloads cannot bypass Pod networking/isolation")
            if doc["kind"] in {"Deployment", "StatefulSet"} and any(
                pod.get("nodeSelector", {}).get(k) != v for k, v in PLACEMENT.items()
            ):
                raise ValueError("Monitoring applications must use protected platform CPU workers")
            if doc["kind"] == "DaemonSet":
                if pod.get("automountServiceAccountToken") is not False or any(
                    not mount.get("readOnly", False)
                    for container in pod.get("containers", [])
                    for mount in container.get("volumeMounts", [])
                ):
                    raise ValueError("Host metrics require no API token and read-only mounts")
                disabled = {
                    "netdev",
                    "netstat",
                    "sockstat",
                    "conntrack",
                    "ipvs",
                    "arp",
                    "netclass",
                    "wifi",
                }
                arguments = set(pod["containers"][0].get("args", []))
                if not {"--no-collector." + name for name in disabled} <= arguments:
                    raise ValueError("Pod network counters must not impersonate host networking")
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


def render(release="monitoring") -> None:
    if release not in {"monitoring", "thanos", *BACKENDS}:
        raise ValueError("Unreviewed telemetry release")
    fetch()
    CACHE.mkdir(parents=True, exist_ok=True)
    values_path = CACHE / (release + "-values.yaml")
    values_path.write_text(
        yaml.safe_dump(
            monitoring_values()
            if release == "monitoring"
            else thanos_values()
            if release == "thanos"
            else backend_values(release),
            sort_keys=False,
        )
    )
    output = subprocess.run(
        [
            tool("helm"),
            "template",
            "klip-" + release,
            str(
                CACHE
                / {"monitoring": "kube-prometheus-stack", "otel": "opentelemetry-collector"}.get(
                    release, release
                )
            ),
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
    if release in BACKENDS:
        expected = image(BACKENDS[release])
        aliases = {
            expected,
            expected.split("@")[0].rsplit(":", 1)[0] + "@" + expected.split("@")[1],
        }
        actual = {
            c["image"]
            for d in documents
            if d["kind"] in {"Deployment", "StatefulSet"}
            for c in (
                d["spec"]["template"]["spec"].get("initContainers", [])
                + d["spec"]["template"]["spec"].get("containers", [])
            )
        }
        if not actual or not actual <= aliases:
            raise ValueError("Unreviewed auxiliary runtime in telemetry component")
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
    (CACHE / (release + "-rendered.yaml")).write_text(output)
    print(f"Rendered and checked {len(documents)} {release} objects; nothing deployed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["fetch", "render"])
    parser.add_argument(
        "--release", choices=["monitoring", "thanos", *BACKENDS], default="monitoring"
    )
    args = parser.parse_args()
    if args.command == "fetch":
        fetch()
    else:
        render(args.release)


if __name__ == "__main__":
    main()
