"""Checksum-locked Helm releases for certificates, projected tokens and artifact mounts."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

import yaml
from common import ROOT, private_dir, source_digest, tf, tool, utc_now, write_private

LOCK = ROOT / "deploy/platform-services.lock.json"
CACHE = ROOT / "artifacts/platform/service-charts"
RELEASES = {
    "cert-manager": "cert-manager",
    "klip-workload-identity": "identity-system",
    "aws-efs-csi-driver": "kube-system",
    "aws-ebs-csi-driver": "kube-system",
    "klip-storage": "klip",
    "cloudnative-pg": "cnpg-system",
    "plugin-barman-cloud": "cnpg-system",
    "klip-image-pull": "klip",
    "klip-observability": "kube-system",
}
PLACEMENT = {"klip.dev.node-restriction.kubernetes.io/platform": "true"}


def fetch() -> None:
    """Only explicit upstream releases from the checked lock can enter the cache."""
    lock = json.loads(LOCK.read_text())
    CACHE.mkdir(parents=True, exist_ok=True)
    for name, item in lock["charts"].items():
        path = CACHE / item["file"]
        if path.name != item["file"]:
            raise ValueError("Chart archive must be a plain filename")
        if not path.exists():
            if "oci" in item:
                if not item["oci"].startswith("oci://quay.io/jetstack/charts/cert-manager@sha256:"):
                    raise ValueError("Unexpected chart registry")
                # Helm's OCI/digest filename differs across releases. Verify the
                # isolated download before placing it at our stable locked path.
                with tempfile.TemporaryDirectory(dir=CACHE, prefix=".oci-") as temporary:
                    subprocess.run(
                        [tool("helm"), "pull", item["oci"], "--destination", temporary], check=True
                    )
                    archives = list(Path(temporary).glob("*.tgz"))
                    if len(archives) != 1 or archives[0].is_symlink():
                        raise ValueError("Expected one regular OCI chart archive")
                    if hashlib.sha256(archives[0].read_bytes()).hexdigest() != item["sha256"]:
                        raise ValueError("OCI chart checksum differs from the reviewed release")
                    os.replace(archives[0], path)
            else:
                sources = {
                    "aws-efs-csi-driver": "https://github.com/kubernetes-sigs/aws-efs-csi-driver/releases/download/helm-chart-",
                    "aws-ebs-csi-driver": "https://github.com/kubernetes-sigs/aws-ebs-csi-driver/releases/download/helm-chart-",
                    "cloudnative-pg": "https://github.com/cloudnative-pg/charts/releases/download/cloudnative-pg-",
                    "plugin-barman-cloud": "https://github.com/cloudnative-pg/charts/releases/download/plugin-barman-cloud-",
                }
                if name not in sources or not item["url"].startswith(sources[name]):
                    raise ValueError("Unexpected chart source")
                with urllib.request.urlopen(item["url"], timeout=60) as response:
                    body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise ValueError("Chart download exceeds its bound")
                path.write_bytes(body)
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("Chart cache checksum differs from reviewed release: " + name)
        with tarfile.open(path) as archive:
            if any(not member.name.startswith(name + "/") for member in archive.getmembers()):
                raise ValueError("Chart archive contains unexpected top-level paths")
            # Extract into an empty directory: an obsolete/tampered template must not
            # survive a verified archive being unpacked over an existing cache.
            with tempfile.TemporaryDirectory(dir=CACHE, prefix=".extract-") as temporary:
                archive.extractall(temporary, filter="data")
                destination = CACHE / name
                if destination.is_symlink():
                    raise ValueError("Refusing a symlinked chart cache")
                if destination.exists():
                    shutil.rmtree(destination)
                os.replace(Path(temporary) / name, destination)
        chart = yaml.safe_load((CACHE / name / "Chart.yaml").read_text())
        if chart["name"] != name or chart["version"] != item["version"]:
            raise ValueError("Cached chart identity differs from reviewed release")


def digest(image: str) -> str:
    value = json.loads(LOCK.read_text())["images"][image]
    if value["platform"] != "linux/amd64" or not re.fullmatch(
        r"sha256:[a-f0-9]{64}", value["digest"]
    ):
        raise ValueError("Missing verified Linux AMD64 image digest")
    return value["digest"]


def values_for(release: str, *, storage: dict | None = None) -> dict:
    if release not in RELEASES:
        raise ValueError("Unreviewed service release")
    if release == "klip-observability":
        return {}
    if release == "klip-image-pull":
        from common import CONFIG

        return {
            "roleArn": f"arn:aws:iam::{CONFIG['account_id']}:role/"
            f"{CONFIG['project']}/klip-images-pull"
        }
    if release in {"cloudnative-pg", "plugin-barman-cloud"}:
        from database_services import values_for_operator

        return values_for_operator(release, digest)
    if release == "aws-ebs-csi-driver":
        from ebs_services import values_for_ebs

        return values_for_ebs(storage, digest)
    if release == "klip-workload-identity":
        image = "public.ecr.aws/eks/amazon-eks-pod-identity-webhook"
        return {"image": image + "@" + digest(image + ":v0.6.19")}
    if release == "cert-manager":
        values = {"crds": {"enabled": True, "keep": True}}
        components = [
            (values, "controller", "cert-manager-controller"),
            (values.setdefault("webhook", {}), "webhook", "cert-manager-webhook"),
            (values.setdefault("cainjector", {}), "cainjector", "cert-manager-cainjector"),
        ]
        for component, role, repository in components:
            labels = {
                "app.kubernetes.io/instance": "cert-manager",
                "app.kubernetes.io/component": role,
            }
            component.update(
                {
                    "replicaCount": 2,
                    "nodeSelector": PLACEMENT,
                    "image": {"digest": digest("quay.io/jetstack/" + repository + ":v1.21.3")},
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "128Mi"},
                        "limits": {"cpu": "1", "memory": "256Mi"},
                    },
                    "podDisruptionBudget": {"enabled": True, "minAvailable": 1},
                    "strategy": {"rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}},
                    "affinity": {
                        "podAntiAffinity": {
                            "requiredDuringSchedulingIgnoredDuringExecution": [
                                {
                                    "topologyKey": "kubernetes.io/hostname",
                                    "labelSelector": {"matchLabels": labels},
                                }
                            ]
                        }
                    },
                    "topologySpreadConstraints": [
                        {
                            "maxSkew": 1,
                            "topologyKey": "topology.kubernetes.io/zone",
                            "whenUnsatisfiable": "DoNotSchedule",
                            "labelSelector": {"matchLabels": labels},
                        }
                    ],
                }
            )
        for name in ("startupapicheck", "acmesolver"):
            values.setdefault(name, {})["image"] = {
                "digest": digest("quay.io/jetstack/cert-manager-" + name + ":v1.21.3")
            }
        values["startupapicheck"]["nodeSelector"] = PLACEMENT
        return values
    if storage is None:
        raise ValueError("EFS requires applied retained-storage allocation")
    if release == "klip-storage":
        return {
            "fileSystemId": storage["artifacts"]["file_system_id"],
            "accessPointId": storage["artifacts"]["access_point_id"],
        }
    role = storage["artifacts"]["node_role_arn"]
    # Do not accept arbitrary roles from a file/env override.
    from common import CONFIG

    if (
        role
        != f"arn:aws:iam::{CONFIG['account_id']}:role/{CONFIG['project']}/klip-storage-artifacts"
    ):
        raise ValueError("Unexpected artifact node role")
    image = "public.ecr.aws/efs-csi-driver/amazon/aws-efs-csi-driver:v3.5.4"
    return {
        "useHelmHooksForCSIDriver": False,
        "controller": {"create": False},  # Static access point owned only by Terraform.
        "image": {"tag": "v3.5.4@" + digest(image)},
        "sidecars": {
            key: {
                "image": {"tag": tag + "@" + digest(repository + ":" + tag)},
                "resources": {
                    "requests": {"cpu": "30m", "memory": "32Mi"},
                    "limits": {"cpu": "200m", "memory": "128Mi"},
                },
            }
            for key, repository, tag in (
                (
                    "livenessProbe",
                    "public.ecr.aws/csi-components/livenessprobe",
                    "v2.20.0-eksbuild.4",
                ),
                (
                    "nodeDriverRegistrar",
                    "public.ecr.aws/csi-components/csi-node-driver-registrar",
                    "v2.18.0-eksbuild.4",
                ),
            )
        },
        "node": {
            "nodeSelector": PLACEMENT,
            "dnsPolicy": "Default",  # Host-network DNS obtains its own AZ-local EFS answer.
            "rollingUpdate": {"maxUnavailable": 1},
            "tolerations": [],
            "serviceAccount": {
                "disableMutation": True,
                "annotations": {
                    "eks.amazonaws.com/role-arn": role,
                    "eks.amazonaws.com/sts-regional-endpoints": "true",
                    "eks.amazonaws.com/token-expiration": "3600",
                },
            },
            "env": [{"name": "AWS_EC2_METADATA_DISABLED", "value": "true"}],
            "resources": {
                "requests": {"cpu": "100m", "memory": "128Mi"},
                "limits": {"cpu": "1", "memory": "512Mi"},
            },
        },
    }


def chart_path(release: str):
    return (
        ROOT / "charts" / release
        if release
        in {"klip-workload-identity", "klip-storage", "klip-image-pull", "klip-observability"}
        else CACHE / release
    )


def post_renderer_arguments(release: str) -> list[str]:
    return (
        [
            "--post-renderer",
            sys.executable,
            "--post-renderer-args",
            str(ROOT / "scripts/infrastructure/efs_static_renderer.py"),
        ]
        if release == "aws-efs-csi-driver"
        else []
    )


def render(release: str, values: dict) -> str:
    process = subprocess.run(
        [
            tool("helm"),
            "template",
            release,
            str(chart_path(release)),
            "--namespace",
            RELEASES[release],
            "--kube-version",
            "1.36.5",
            "--values",
            "-",
            *post_renderer_arguments(release),
        ],
        input=yaml.safe_dump(values),
        text=True,
        capture_output=True,
        check=True,
    )
    return process.stdout


def deploy(release: str) -> dict:
    from cluster import FOLDER, checked, kubeconfigs, kubectl, tunnel
    from common import PROFILES, Aws

    if release not in RELEASES:
        raise ValueError("Unreviewed service release")
    Aws(PROFILES["foundation"]).preflight("foundation")
    fetch()
    expected = json.loads((FOLDER / "platform-placement.json").read_text())["nodes"]
    current = {
        item["metadata"]["name"]: item
        for item in json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    }
    for name, observation in expected.items():
        node = current.get(name, {})
        if (
            node.get("metadata", {}).get("uid") != observation["uid"]
            or node["metadata"]["labels"].get(next(iter(PLACEMENT))) != "true"
            or not any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in node.get("status", {}).get("conditions", [])
            )
        ):
            raise ValueError("All three protected platform identities must be Ready before rollout")
    if len(expected) != 3:
        raise ValueError("Three independently protected platform hosts are required")
    if release == "klip-observability":
        for namespace in ("observability", "observability-node-agents"):
            existing = kubectl(
                "get", "namespace", namespace, "--ignore-not-found", "-o", "json"
            ).strip()
            if existing:
                metadata = json.loads(existing)["metadata"]
                if (
                    metadata.get("labels", {}).get("klip.dev/managed-by") != "klip-observability"
                    or metadata.get("annotations", {}).get("meta.helm.sh/release-name")
                    != "klip-observability"
                    or metadata.get("annotations", {}).get("meta.helm.sh/release-namespace")
                    != "kube-system"
                ):
                    raise ValueError("Refuse to adopt an existing unowned telemetry namespace")
    storage = None
    if release in {"aws-efs-csi-driver", "aws-ebs-csi-driver", "klip-storage"}:
        from platform_storage import verify_storage

        storage = verify_storage()["allocation"]
        if release == "aws-ebs-csi-driver":
            from ebs_services import verify_ebs_identity

            storage = verify_ebs_identity()
        pods = json.loads(
            kubectl(
                "-n", "identity-system", "get", "deployment", "klip-workload-identity", "-o", "json"
            )
        )
        if pods.get("status", {}).get("availableReplicas", 0) < 2:
            raise ValueError("The HA token webhook must be ready before EFS")
        if release == "klip-storage":
            driver = json.loads(
                kubectl("-n", "kube-system", "get", "daemonset", "efs-csi-node", "-o", "json")
            )
            if (
                driver.get("status", {}).get("numberReady") != 3
                or driver["status"].get("observedGeneration") != driver["metadata"]["generation"]
            ):
                raise ValueError("All three EFS node plugins must be current and Ready")
    if release == "cloudnative-pg":
        from cluster import Host, load_hosts

        namespace = yaml.safe_load((ROOT / "deploy/releases/platform-namespaces.yaml").read_text())
        existing = kubectl(
            "get", "namespace", "platform-data", "--ignore-not-found", "-o", "json"
        ).strip()
        if existing:
            labels = json.loads(existing)["metadata"].get("labels", {})
            if any(labels.get(k) != v for k, v in namespace["metadata"]["labels"].items()):
                raise ValueError(
                    "Existing data namespace does not match the reviewed project/security boundary"
                )
        else:
            Host(load_hosts()["cp-01"]).run(
                ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "create", "-f", "-"],
                data=yaml.safe_dump(namespace),
            )
    values = values_for(release, storage=storage)
    rendered = render(release, values)
    path = FOLDER / "service-values" / (release + ".yaml")
    write_private(path, yaml.safe_dump(values))
    write_private(path.with_suffix(".rendered.yaml"), rendered)
    kubeconfigs()
    with tunnel():
        arguments = [
            tool("helm"),
            "upgrade",
            "--install",
            release,
            str(chart_path(release)),
            "--namespace",
            RELEASES[release],
            "--create-namespace",
            "--kubeconfig",
            str(FOLDER / "operator.conf"),
            "--values",
            str(path),
            "--atomic",
            "--wait",
            "--timeout",
            "10m",
            "--history-max",
            "10",
        ]
        arguments += post_renderer_arguments(release)
        result = checked(arguments, timeout=750)
    report = {
        "release": release,
        "namespace": RELEASES[release],
        "observed_at": utc_now(),
        "source_sha256": source_digest(),
        "helm_completed": True,
    }
    write_private(path.with_suffix(".helm.log"), result)
    write_private(FOLDER / "service-releases" / (release + ".json"), report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["fetch", "render", "deploy"])
    parser.add_argument("--release", choices=list(RELEASES))
    args = parser.parse_args()
    if args.command == "fetch":
        fetch()
    else:
        if not args.release:
            parser.error("--release is required")
        if args.command == "deploy":
            from cluster import FOLDER

            private_dir(FOLDER)
            with (FOLDER / "operator.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                print(json.dumps(deploy(args.release), indent=2))
        else:
            storage = (
                json.loads(tf("storage", "output", "-json", "storage_manifest"))
                if args.release in {"aws-efs-csi-driver", "klip-storage"}
                else None
            )
            if args.release == "aws-ebs-csi-driver":
                storage = json.loads(
                    tf("storage-access", "output", "-json", "storage_access_manifest")
                )
            print(render(args.release, values_for(args.release, storage=storage)))
