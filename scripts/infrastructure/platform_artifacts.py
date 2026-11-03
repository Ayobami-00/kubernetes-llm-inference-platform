"""Freeze upstream release artifacts once; install only from the reviewed checksum lock."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from common import ROOT

LOCK = ROOT / "deploy/artifacts.lock.json"
CACHE = ROOT / "artifacts/platform"
VERSIONS = {
    "kubernetes": "1.36.5",
    "cilium": "1.20.4",
    "containerd": "2.4.2",
    "runc": "1.5.2",
    "helm": "3.22.1",
    "helmfile": "1.8.2",
    "age": "1.3.2",
    "metrics-server-chart": "3.14.0",
    "ansible-core": "2.21.6",
    "ansible-lint": "26.10.1",
    "hubble": "1.20.2",
    "session-manager-plugin": "1.2.896.0",
    "metadata-guard-chart": "0.1.0",
}


def fetch(url: str) -> bytes:
    if not url.startswith("https://") or "latest" in url:
        raise ValueError("Release URLs must use HTTPS and an explicit version")
    request = urllib.request.Request(url, headers={"User-Agent": "klip-infrastructure/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def specifications() -> dict[str, dict]:
    result = {}
    for target in ("darwin-arm64", "linux-amd64"):
        os_name, arch = target.split("-")
        for name, url, member in (
            (
                "helm",
                f"https://get.helm.sh/helm-v{VERSIONS['helm']}-{target}.tar.gz",
                f"{target}/helm",
            ),
            (
                "helmfile",
                f"https://github.com/helmfile/helmfile/releases/download/v{VERSIONS['helmfile']}/helmfile_{VERSIONS['helmfile']}_{os_name}_{arch}.tar.gz",
                "helmfile",
            ),
            (
                "age",
                f"https://github.com/FiloSottile/age/releases/download/v{VERSIONS['age']}/age-v{VERSIONS['age']}-{target}.tar.gz",
                "age/age",
            ),
        ):
            result[f"{name}-{target}"] = {"url": url, "member": member, "tool": name}
        result[f"kubectl-{target}"] = {
            "url": f"https://dl.k8s.io/release/v{VERSIONS['kubernetes']}/bin/{os_name}/{arch}/kubectl",
            "tool": "kubectl",
        }
        result[f"hubble-{target}"] = {
            "url": f"https://github.com/cilium/hubble/releases/download/v{VERSIONS['hubble']}/hubble-{target}.tar.gz",
            "member": "hubble",
            "tool": "hubble",
        }
    result["containerd-linux-amd64"] = {
        "url": f"https://github.com/containerd/containerd/releases/download/v{VERSIONS['containerd']}/containerd-{VERSIONS['containerd']}-linux-amd64.tar.gz",
        "member": "bin/containerd",
        "tool": "containerd",
    }
    result["runc-linux-amd64"] = {
        "url": f"https://github.com/opencontainers/runc/releases/download/v{VERSIONS['runc']}/runc.amd64",
        "tool": "runc",
    }
    result["cilium-chart"] = {"url": f"https://helm.cilium.io/cilium-{VERSIONS['cilium']}.tgz"}
    result["metrics-server-chart"] = {
        "url": "https://github.com/kubernetes-sigs/metrics-server/releases/download/metrics-server-helm-chart-3.14.0/metrics-server-3.14.0.tgz"
    }
    result["session-manager-plugin-darwin-arm64"] = {
        "url": f"https://s3.amazonaws.com/session-manager-downloads/plugin/{VERSIONS['session-manager-plugin']}/mac_arm64/sessionmanager-bundle.zip",
        "member": "sessionmanager-bundle/bin/session-manager-plugin",
        "tool": "session-manager-plugin",
        "format": "zip",
    }
    result["session-manager-plugin-linux-amd64"] = {
        "url": f"https://s3.amazonaws.com/session-manager-downloads/plugin/{VERSIONS['session-manager-plugin']}/ubuntu_64bit/session-manager-plugin.deb",
        "tool": "session-manager-plugin",
        "format": "deb",
    }
    result["kubernetes-release-key"] = {
        "url": "https://pkgs.k8s.io/core:/stable:/v1.36/deb/Release.key"
    }
    return result


def freeze() -> None:
    if LOCK.exists():
        raise ValueError("Artifact lock already exists; changes require an explicit version review")
    items = specifications()
    tree = json.loads(
        fetch(
            f"https://api.github.com/repos/cilium/cilium/git/trees/v{VERSIONS['cilium']}?recursive=1"
        )
    )
    crds = [
        item["path"]
        for item in tree["tree"]
        if item["path"].startswith("pkg/k8s/apis/cilium.io/client/crds/")
        and item["path"].endswith(".yaml")
    ]
    if not crds or tree.get("truncated"):
        raise ValueError("Incomplete Cilium CRD source tree")
    for path in crds:
        items["crd-" + Path(path).stem] = {
            "url": f"https://raw.githubusercontent.com/cilium/cilium/v{VERSIONS['cilium']}/{path}",
            "crd": True,
        }
    CACHE.mkdir(parents=True, exist_ok=True)
    for name, item in items.items():
        payload = fetch(item["url"])
        item["sha256"] = hashlib.sha256(payload).hexdigest()
        (CACHE / name).write_bytes(payload)
        print(f"Frozen {name}", flush=True)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(json.dumps({"versions": VERSIONS, "artifacts": items}, indent=2) + "\n")


def install(*, all_targets: bool = False) -> None:
    lock = json.loads(LOCK.read_text())
    if lock["versions"] != VERSIONS:
        raise ValueError("Version/artifact lock drift")
    target = (
        f"{platform.system().lower()}-"
        + {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64"}[platform.machine()]
    )
    CACHE.mkdir(parents=True, exist_ok=True)
    for name, item in lock["artifacts"].items():
        if not all_targets and "tool" in item and not name.endswith(target):
            continue
        path = CACHE / name
        if not path.exists():
            path.write_bytes(fetch(item["url"]))
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != item["sha256"]:
            raise ValueError(f"Artifact checksum mismatch: {name}")
        if item.get("crd"):
            folder = CACHE / "crds"
            folder.mkdir(exist_ok=True)
            (folder / f"{name}.yaml").write_bytes(payload)
        if name.endswith("-chart"):
            (CACHE / f"{name}.tgz").write_bytes(payload)
            folder = CACHE / "charts"
            folder.mkdir(exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
                archive.extractall(folder, filter="data")
        if "tool" in item and name.endswith(target):
            destination = ROOT / "artifacts/tools/bin" / item["tool"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            if item.get("format") == "zip":
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    binary = archive.read(item["member"])
            elif item.get("format") == "deb":
                members = subprocess.check_output(["ar", "t", str(path)], text=True).splitlines()
                member = next(name for name in members if name.startswith("data.tar"))
                data = subprocess.check_output(["ar", "p", str(path), member])
                with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                    name = next(
                        name
                        for name in archive.getnames()
                        if name.endswith("/bin/session-manager-plugin")
                    )
                    binary = archive.extractfile(name).read()
            elif item.get("member"):
                with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
                    binary = archive.extractfile(item["member"]).read()
            else:
                binary = payload
            temporary = destination.with_suffix(".new")
            temporary.write_bytes(binary)
            temporary.chmod(0o755)
            os.replace(temporary, destination)
            if item["tool"] == "age":
                with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
                    keygen = destination.with_name("age-keygen")
                    keygen.write_bytes(archive.extractfile("age/age-keygen").read())
                    keygen.chmod(0o755)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["freeze", "install"])
    parser.add_argument("--all-targets", action="store_true")
    arguments = parser.parse_args()
    freeze() if arguments.command == "freeze" else install(all_targets=arguments.all_targets)
