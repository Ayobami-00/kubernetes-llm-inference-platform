"""Checksum-locked GPU Operator install, confined to explicitly admitted GPU hosts.

Ansible owns the OS/containerd and kubeadm; this release owns every NVIDIA host
component. Rendering does not establish GPU readiness. The executor must perform
device, network and telemetry checks before removing the bootstrap quarantine.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import yaml
from common import ROOT, private_dir, source_digest, state_home, tool, utc_now, write_private

LOCK = ROOT / "deploy/gpu-operator.lock.json"
CACHE = ROOT / "artifacts/platform/gpu-operator-charts"
NAMESPACE = "gpu-system"
RELEASE = "gpu-operator"
NFD_RELEASE = "gpu-node-features"
PLACEMENT = {"klip.dev.node-restriction.kubernetes.io/platform": "true"}
CANDIDATE = {"klip.dev.node-restriction.kubernetes.io/gpu-candidate": "true"}
BOOTSTRAP = {
    "key": "klip.dev/bootstrap",
    "operator": "Equal",
    "value": "true",
    "effect": "NoSchedule",
}
GPU_TOLERATION = {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}


def sha(body):
    return hashlib.sha256(body).hexdigest()


def settings():
    value = json.loads(LOCK.read_text())
    chart = value["chart"]
    if (
        value["schema_version"] != "klip-gpu-operator.v1"
        or chart["name"] != RELEASE
        or chart["version"] != "v26.7.2"
        or chart["file"] != "gpu-operator-v26.7.2.tgz"
        or chart["url"] != "https://helm.ngc.nvidia.com/nvidia/charts/" + chart["file"]
        or not re.fullmatch(r"[a-f0-9]{64}", chart["sha256"])
    ):
        raise ValueError("Unreviewed GPU Operator chart identity")
    nfd = value["nfd_chart"]
    if (
        nfd["name"] != "node-feature-discovery"
        or nfd["version"] != "0.20.1"
        or nfd["file"] != "node-feature-discovery-0.20.1.tgz"
        or not re.fullmatch(
            r"oci://registry.k8s.io/nfd/charts/node-feature-discovery@sha256:[a-f0-9]{64}",
            nfd["oci"],
        )
        or not re.fullmatch(r"[a-f0-9]{64}", nfd["sha256"])
    ):
        raise ValueError("Unreviewed NFD chart identity")
    for reference, item in value["images"].items():
        if (
            not reference.startswith(("nvcr.io/nvidia/", "registry.k8s.io/nfd/"))
            or item["platform"] != "linux/amd64"
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", item["digest"])
        ):
            raise ValueError("GPU component requires a verified Linux AMD64 digest")
    return value


def image(reference):
    return reference + "@" + settings()["images"][reference]["digest"]


class ChartRedirect(urllib.request.HTTPRedirectHandler):
    """NGC redirects to its public artifact host; never follow arbitrary origins."""

    def redirect_request(self, request, fp, code, message, headers, new_url):
        parsed = urlsplit(new_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"helm.ngc.nvidia.com", "xfiles.ngc.nvidia.com"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
        ):
            raise ValueError("GPU chart redirect left the reviewed public hosts")
        return super().redirect_request(request, fp, code, message, headers, new_url)


def fetch_chart(item):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / item["file"]
    if path.is_symlink():
        raise ValueError("Refusing a symlinked chart archive")
    if not path.exists():
        if "oci" in item:
            with tempfile.TemporaryDirectory(dir=CACHE, prefix=".oci-") as temporary:
                subprocess.run(
                    [tool("helm"), "pull", item["oci"], "--destination", temporary],
                    check=True,
                    capture_output=True,
                    timeout=60,
                )
                files = list(Path(temporary).glob("*.tgz"))
                if (
                    len(files) != 1
                    or files[0].is_symlink()
                    or files[0].stat().st_size > 8 * 1024 * 1024
                ):
                    raise ValueError("NFD chart download exceeded its bound")
                body = files[0].read_bytes()
        else:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), ChartRedirect())
            with opener.open(item["url"], timeout=30) as response:
                body = response.read(8 * 1024 * 1024 + 1)
        if len(body) > 8 * 1024 * 1024 or sha(body) != item["sha256"]:
            raise ValueError("GPU chart download exceeded its bound or checksum differs")
        descriptor, temporary = tempfile.mkstemp(prefix=".download-", dir=CACHE)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass  # The checksum below also verifies a concurrent writer.
        finally:
            Path(temporary).unlink(missing_ok=True)
    if path.stat().st_size > 8 * 1024 * 1024 or sha(path.read_bytes()) != item["sha256"]:
        raise ValueError("GPU chart cache checksum differs")
    with tempfile.TemporaryDirectory(dir=CACHE, prefix=".chart-") as temporary:
        with tarfile.open(path) as archive:
            members = archive.getmembers()
            if len(members) > 3000 or sum(m.size for m in members) > 32 * 1024 * 1024:
                raise ValueError("GPU chart unpacking exceeded its bound")
            for member in members:
                name = PurePosixPath(member.name)
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or name.parts[0] != item["name"]
                    or not (member.isfile() or member.isdir())
                ):
                    raise ValueError("Unsafe GPU chart archive entry")
            archive.extractall(temporary, filter="data")
        source = Path(temporary) / item["name"]
        chart = yaml.safe_load((source / "Chart.yaml").read_text())
        if chart["name"] != item["name"] or chart["version"] != item["version"]:
            raise ValueError("GPU chart metadata differs")
        destination = CACHE / item["name"]
        if destination.is_symlink():
            raise ValueError("Refusing a symlinked chart directory")
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(source, destination)
    return destination


def fetch():
    lock = settings()
    for name in ("chart", "nfd_chart"):
        fetch_chart(lock[name])
    return CACHE


def version(reference):
    # Tag@digest also works with the chart's colon-based controller helpers.
    # resolveDriverTag in the pinned operator skips its OS suffix for digests;
    # the lock therefore explicitly selects the ubuntu24.04 driver image.
    return reference.rsplit(":", 1)[1] + "@" + settings()["images"][reference]["digest"]


def resources(cpu, memory, limit):
    return {"requests": {"cpu": cpu, "memory": memory}, "limits": {"memory": limit}}


def values():
    operator = "nvcr.io/nvidia/gpu-operator:v26.7.2"
    tolerations = [GPU_TOLERATION, BOOTSTRAP]
    result = {
        "operator": {
            "version": version(operator),
            "nodeSelector": PLACEMENT,
            "tolerations": [],
            "affinity": {},
            "cleanupCRD": False,
            "upgradeCRD": False,
            "resources": resources("200m", "128Mi", "512Mi"),
        },
        "daemonsets": {"tolerations": tolerations},
        "driver": {
            "version": version("nvcr.io/nvidia/driver:595.91.07-ubuntu24.04"),
            "kernelModuleType": "open",
            "usePrecompiled": False,
            "upgradePolicy": {"autoUpgrade": False},
            "manager": {
                "version": version("nvcr.io/nvidia/cloud-native/k8s-driver-manager:v0.12.1")
            },
            "resources": resources("200m", "256Mi", "2Gi"),
        },
        "validator": {"version": version(operator), "resources": resources("50m", "64Mi", "256Mi")},
        "mig": {"strategy": "none"},
        "migManager": {"enabled": False},
        "dcgm": {"enabled": False},
        "gds": {"enabled": False},
        "gdrcopy": {"enabled": False},
        "nodeStatusExporter": {"enabled": False},
        **{
            key: {"enabled": False}
            for key in (
                "vgpuManager",
                "vgpuDeviceManager",
                "vfioManager",
                "ccManager",
                "kataManager",
                "sandboxDevicePlugin",
                "kataSandboxDevicePlugin",
            )
        },
        "nfd": {"enabled": False, "nodefeaturerules": False},
    }
    for key, reference in {
        "toolkit": "nvcr.io/nvidia/k8s/container-toolkit:v1.20.1",
        "devicePlugin": "nvcr.io/nvidia/k8s-device-plugin:v0.20.1",
        "gfd": "nvcr.io/nvidia/k8s-device-plugin:v0.20.1",
        "dcgmExporter": "nvcr.io/nvidia/k8s/dcgm-exporter:4.6.1-4.8.4-distroless",
    }.items():
        result[key] = {
            "version": version(reference),
            "resources": resources("50m", "128Mi", "512Mi"),
        }
    result["dcgmExporter"].update(
        serviceMonitor={
            "enabled": True,
            "honorLabels": False,
            "additionalLabels": {"release": "klip-monitoring"},
        },
        enablePodUID=True,
    )
    return result


def nfd_values():
    return {
        "image": {"tag": version("registry.k8s.io/nfd/node-feature-discovery:v0.20.1")},
        "postDeleteCleanup": False,
        "worker": {"nodeSelector": CANDIDATE, "tolerations": [GPU_TOLERATION, BOOTSTRAP]},
        "master": {"nodeSelector": PLACEMENT, "tolerations": [], "affinity": {}},
        "gc": {"nodeSelector": PLACEMENT, "tolerations": []},
        "topologyUpdater": {"enable": False},
    }


def releases():
    return [(NFD_RELEASE, "node-feature-discovery", nfd_values()), (RELEASE, RELEASE, values())]


def effective_values():
    return {release: value for release, _, value in releases()}


def boundary():
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": NAMESPACE,
            "labels": {
                "app.kubernetes.io/part-of": "klip",
                "pod-security.kubernetes.io/enforce": "privileged",
                "pod-security.kubernetes.io/enforce-version": "v1.36",
            },
        },
    }
    # NVIDIA host operands need kernel/device mounts. This exception is isolated
    # from management/runner namespaces; global IMDS deny remains authoritative.
    network = {
        "apiVersion": "cilium.io/v2",
        "kind": "CiliumNetworkPolicy",
        "metadata": {"name": "gpu-components", "namespace": NAMESPACE},
        "spec": {
            "endpointSelector": {},
            "ingress": [
                {
                    "fromEndpoints": [
                        {"matchLabels": {"k8s:io.kubernetes.pod.namespace": NAMESPACE}}
                    ],
                },
                {
                    "fromEndpoints": [
                        {
                            "matchLabels": {
                                "k8s:io.kubernetes.pod.namespace": "observability",
                                "app.kubernetes.io/name": "prometheus",
                            }
                        }
                    ],
                    "toPorts": [{"ports": [{"port": "9400", "protocol": "TCP"}]}],
                },
            ],
            "egress": [
                {
                    "toEntities": ["kube-apiserver"],
                    "toPorts": [
                        {
                            "ports": [
                                {"port": "443", "protocol": "TCP"},
                                {"port": "6443", "protocol": "TCP"},
                            ]
                        }
                    ],
                },
                {
                    "toEndpoints": [
                        {
                            "matchLabels": {
                                "k8s:io.kubernetes.pod.namespace": "kube-system",
                                "k8s:k8s-app": "kube-dns",
                            }
                        }
                    ],
                    "toPorts": [
                        {
                            "ports": [
                                {"port": "53", "protocol": "UDP"},
                                {"port": "53", "protocol": "TCP"},
                            ]
                        }
                    ],
                },
                {"toEndpoints": [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": NAMESPACE}}]},
            ],
        },
    }
    driver_network = {
        "apiVersion": "cilium.io/v2",
        "kind": "CiliumNetworkPolicy",
        "metadata": {"name": "driver-package-downloads", "namespace": NAMESPACE},
        "spec": {
            "endpointSelector": {"matchLabels": {"app": "nvidia-driver-daemonset"}},
            "egress": [
                {
                    "toEntities": ["world"],
                    "toPorts": [
                        {
                            "ports": [
                                {"port": "443", "protocol": "TCP"},
                                {"port": "80", "protocol": "TCP"},
                            ]
                        }
                    ],
                }
            ],
        },
    }
    prometheus_network = {
        "apiVersion": "cilium.io/v2",
        "kind": "CiliumNetworkPolicy",
        "metadata": {"name": "prometheus-gpu-exporters", "namespace": "observability"},
        "spec": {
            "endpointSelector": {"matchLabels": {"app.kubernetes.io/name": "prometheus"}},
            "egress": [
                {
                    "toEndpoints": [
                        {
                            "matchLabels": {
                                "k8s:io.kubernetes.pod.namespace": NAMESPACE,
                                "app": "nvidia-dcgm-exporter",
                            }
                        }
                    ],
                    "toPorts": [{"ports": [{"port": "9400", "protocol": "TCP"}]}],
                }
            ],
        },
    }
    return yaml.safe_dump_all([namespace, network, driver_network, prometheus_network])


def render():
    rendered = []
    for release, chart, value in releases():
        process = subprocess.run(
            [
                tool("helm"),
                "template",
                release,
                str(CACHE / chart),
                "--namespace",
                NAMESPACE,
                "--kube-version",
                "1.36.5",
                "--include-crds",
                "--values",
                "-",
            ],
            input=yaml.safe_dump(value),
            text=True,
            capture_output=True,
            check=True,
        )
        rendered.append(process.stdout)
    output = "\n---\n".join(rendered)
    validate_render(output)
    return output


def validate_render(text):
    documents = [d for d in yaml.safe_load_all(text) if d]
    seen, policies = set(), []
    for document in documents:
        metadata, kind = document["metadata"], document["kind"]
        key = (kind, metadata.get("namespace"), metadata["name"])
        if key in seen or metadata.get("namespace", NAMESPACE) != NAMESPACE:
            raise ValueError("GPU release has duplicate or foreign-namespace objects")
        seen.add(key)
        if kind == "ClusterPolicy":
            policies.append(document)
        if kind in {"Deployment", "DaemonSet", "Job"}:
            pod = document["spec"]["template"]["spec"]
            selector = CANDIDATE if kind == "DaemonSet" else PLACEMENT
            if pod.get("nodeSelector") not in [selector, {**selector, "kubernetes.io/os": "linux"}]:
                raise ValueError("GPU/NFD workload placement exceeds its admitted hosts")
            for container in pod.get("initContainers", []) + pod["containers"]:
                if container["image"] not in {image(r) for r in settings()["images"]}:
                    raise ValueError("GPU workload image is not locked")
                for env in container.get("env", []):
                    if env["name"] in {"DRIVER_MANAGER_IMAGE", "VALIDATOR_IMAGE"} and env.get(
                        "value"
                    ) not in {image(r) for r in settings()["images"]}:
                        raise ValueError("GPU operator child image is not locked")
    if len(policies) != 1:
        raise ValueError("Exactly one cluster GPU policy is required")
    spec = policies[0]["spec"]
    for key in ("driver", "toolkit", "devicePlugin", "gfd", "dcgmExporter", "validator"):
        component = spec[key]
        reference = component["repository"] + "/" + component["image"] + ":" + component["version"]
        if reference not in {image(r) for r in settings()["images"]}:
            raise ValueError("GPU policy operand image is not locked")
    if spec["driver"]["upgradePolicy"]["autoUpgrade"] or spec["mig"]["strategy"] != "none":
        raise ValueError("Unreviewed GPU upgrade or partitioning mode")
    for key in (
        "migManager",
        "dcgm",
        "gds",
        "gdrcopy",
        "vgpuManager",
        "vgpuDeviceManager",
        "vfioManager",
        "ccManager",
        "kataManager",
        "sandboxWorkloads",
        "kataSandboxDevicePlugin",
        "nodeStatusExporter",
    ):
        if spec[key].get("enabled", False):
            raise ValueError("Unreviewed optional GPU service enabled")
    return documents


def folder():
    return private_dir(state_home() / "cluster/gpu-operator")


def observe_initial(kubectl):
    nodes = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    expected = {"cp-01", "cp-02", "cp-03", "platform-01", "platform-02", "platform-03"}
    if {n["metadata"]["name"] for n in nodes} != expected or any(
        n["metadata"].get("labels", {}).get(next(iter(CANDIDATE)))
        or "nvidia.com/gpu" in n.get("status", {}).get("capacity", {})
        or any(k.startswith("feature.node.kubernetes.io/") for k in n["metadata"].get("labels", {}))
        for n in nodes
    ):
        raise ValueError(
            "Initial GPU release requires the six CPU hosts and no existing NFD/GPU ownership"
        )
    deployments = json.loads(kubectl("get", "deployments,daemonsets", "-A", "-o", "json"))["items"]
    if any(
        "node-feature-discovery" in d["metadata"]["name"] or d["metadata"]["name"] == RELEASE
        for d in deployments
    ):
        raise ValueError("Refusing a duplicate GPU Operator or NFD installation")
    if kubectl("get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "json").strip():
        raise ValueError("GPU namespace already exists; reconcile its retained release")
    for name in (
        "clusterpolicies.nvidia.com",
        "gpuclusters.nvidia.com",
        "nodefeatures.nfd.k8s-sigs.io",
    ):
        if kubectl("get", "crd", name, "--ignore-not-found", "-o", "name").strip():
            raise ValueError("Existing GPU/NFD CRDs require explicit ownership reconciliation")
    return {
        "kube_system_uid": json.loads(kubectl("get", "namespace", "kube-system", "-o", "json"))[
            "metadata"
        ]["uid"],
        "nodes": {n["metadata"]["name"]: n["metadata"]["uid"] for n in nodes},
    }


def prepare(security_directory):
    from cluster import kubectl
    from gpu_operator_security import assess

    if (folder() / "active.json").exists():
        raise ValueError("GPU Operator is already installed; use an explicit upgrade procedure")
    fetch()
    rendered = render()
    plan = {
        "schema_version": "klip-gpu-operator-install.v1",
        "created_at": utc_now(),
        "source_sha256": source_digest(),
        "lock_sha256": sha(LOCK.read_bytes()),
        "values_sha256": sha(yaml.safe_dump(effective_values()).encode()),
        "rendered_sha256": sha(rendered.encode()),
        "boundary_sha256": sha(boundary().encode()),
        "cluster": observe_initial(kubectl),
        "security_directory": str(security_directory.resolve()),
        "security": assess(security_directory),
        "privileges": (
            "NVIDIA operator cluster node/operand RBAC; privileged host operands in gpu-system only"
        ),
        "gpu_capacity_proven": False,
    }
    plan["approval_sha256"] = sha(json.dumps(plan, sort_keys=True).encode())
    target = private_dir(folder() / "plans" / plan["approval_sha256"])
    write_private(target / "plan.json", plan)
    write_private(target / "values.yaml", yaml.safe_dump(values()))
    write_private(target / "nfd-values.yaml", yaml.safe_dump(nfd_values()))
    write_private(target / "rendered.yaml", rendered)
    write_private(target / "boundary.yaml", boundary())
    return plan


def apply(approval):
    from cluster import FOLDER, Host, checked, kubeconfigs, kubectl, load_hosts, tunnel
    from gpu_operator_security import assess

    if not re.fullmatch(r"[a-f0-9]{64}", approval or ""):
        raise ValueError("A saved GPU install plan is required")
    target = folder() / "plans" / approval
    plan = json.loads((target / "plan.json").read_text())
    unsigned = {k: v for k, v in plan.items() if k != "approval_sha256"}
    if (
        sha(json.dumps(unsigned, sort_keys=True).encode()) != approval
        or plan["approval_sha256"] != approval
        or source_digest() != plan["source_sha256"]
        or sha(LOCK.read_bytes()) != plan["lock_sha256"]
        or sha(yaml.safe_dump(effective_values()).encode()) != plan["values_sha256"]
        or yaml.safe_load((target / "values.yaml").read_text()) != values()
        or yaml.safe_load((target / "nfd-values.yaml").read_text()) != nfd_values()
        or sha(boundary().encode()) != plan["boundary_sha256"]
        or sha((target / "boundary.yaml").read_bytes()) != plan["boundary_sha256"]
        or sha((target / "rendered.yaml").read_bytes()) != plan["rendered_sha256"]
        or (folder() / "active.json").exists()
        or (target / "result.json").exists()
        or assess(Path(plan["security_directory"])) != plan["security"]
    ):
        raise ValueError(
            "GPU install plan changed or already executed; reconcile rather than replay"
        )
    fetch()
    if sha(render().encode()) != plan["rendered_sha256"]:
        raise ValueError("GPU chart rendering changed")
    kubeconfigs()
    result = {"approval_sha256": approval, "status": "running", "started_at": utc_now()}
    with (FOLDER / "operator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if observe_initial(kubectl) != plan["cluster"]:
            raise ValueError("GPU install cluster identities changed")
        write_private(target / "result.json", result)
        try:
            Host(load_hosts()["cp-01"]).run(
                ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "create", "-f", "-"],
                data=boundary(),
            )
            with tunnel():
                for release, chart, _ in releases():
                    result["current_release"] = release
                    write_private(target / "result.json", result)
                    output = checked(
                        [
                            tool("helm"),
                            "upgrade",
                            "--install",
                            release,
                            str(CACHE / chart),
                            "--namespace",
                            NAMESPACE,
                            "--kubeconfig",
                            str(FOLDER / "operator.conf"),
                            "--values",
                            str(
                                target
                                / ("values.yaml" if release == RELEASE else "nfd-values.yaml")
                            ),
                            "--atomic",
                            "--wait",
                            "--timeout",
                            "8m",
                            "--history-max",
                            "10",
                        ],
                        timeout=540,
                    )
                    write_private(target / (release + ".helm.log"), output)
                    result.setdefault("completed_releases", []).append(release)
                    write_private(target / "result.json", result)
            result.update(status="deployed", deployed_at=utc_now(), gpu_capacity_proven=False)
            write_private(folder() / "active.json", {**plan, **result})
        except Exception as error:
            result.update(status="reconciliation_required", error_type=type(error).__name__)
            raise
        finally:
            write_private(target / "result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["fetch", "render", "prepare", "apply"])
    parser.add_argument("--approve-plan")
    parser.add_argument("--security-directory", type=Path)
    args = parser.parse_args()
    if args.command == "fetch":
        print(fetch())
    elif args.command == "render":
        print(render())
    else:
        if args.command == "prepare" and args.security_directory is None:
            parser.error("--security-directory is required for prepare")
        print(
            json.dumps(
                prepare(args.security_directory)
                if args.command == "prepare"
                else apply(args.approve_plan),
                indent=2,
            )
        )
