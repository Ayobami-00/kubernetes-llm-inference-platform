"""Install only the checksummed Linux tools needed by the isolated host executor."""

import hashlib
import io
import json
import os
import subprocess
import tarfile
import urllib.request

from common import ROOT

REQUIRED = (
    "kubernetes-release-key",
    "runc-linux-amd64",
    "session-manager-plugin-linux-amd64",
)
MAX_BYTES = 128 * 1024 * 1024


def download(item):
    if not item["url"].startswith("https://") or "latest" in item["url"]:
        raise ValueError("Executor tools require fixed HTTPS releases")
    with urllib.request.urlopen(item["url"], timeout=60) as response:
        payload = response.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES or hashlib.sha256(payload).hexdigest() != item["sha256"]:
        raise ValueError("Executor tool size or checksum differs from its release lock")
    return payload


def install():
    lock = json.loads((ROOT / "deploy/artifacts.lock.json").read_text())
    cache = ROOT / "artifacts/platform"
    cache.mkdir(parents=True, exist_ok=True)
    for name in REQUIRED:
        item = lock["artifacts"][name]
        path = cache / name
        payload = download(item)
        path.write_bytes(payload)
        if item.get("format") == "deb":
            members = subprocess.check_output(["ar", "t", str(path)], text=True).splitlines()
            selected = [name for name in members if name.startswith("data.tar")]
            if len(selected) != 1:
                raise ValueError("Unexpected session-manager package structure")
            packed = subprocess.check_output(["ar", "p", str(path), selected[0]])
            with tarfile.open(fileobj=io.BytesIO(packed)) as archive:
                selected = [
                    m
                    for m in archive.getmembers()
                    if m.name.endswith("/bin/session-manager-plugin")
                ]
                if len(selected) != 1 or not selected[0].isfile():
                    raise ValueError("Session manager must be a single regular executable")
                destination = ROOT / "artifacts/tools/bin/session-manager-plugin"
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.extractfile(selected[0]).read())
                destination.chmod(0o755)
    from gpu_runtime import install_artifact

    install_artifact()
    os.chmod(cache / "runc-linux-amd64", 0o755)


def verify_installed():
    """Exercise the deployed import/configuration closure without host access.

    An image built from only part of the infrastructure tree must fail during
    its build, rather than discovering a missing lock or archive on a paid VM.
    This performs no cloud calls, downloads, or state/custody changes.
    """
    from gpu_operator import settings as operator_settings
    from gpu_runtime import settings, verify
    from host_executor_recovery import fenced_chain  # noqa: F401
    from platform_management.host_executor import inspection, worker  # noqa: F401
    from workload_identity import configuration

    configuration()
    operator_settings()
    value = settings()
    verify(
        ROOT / "artifacts/platform" / value["containerd_artifact"]["filename"],
        value["containerd_artifact"],
    )
    lock = json.loads((ROOT / "deploy/artifacts.lock.json").read_text())
    for name in REQUIRED:
        body = (ROOT / "artifacts/platform" / name).read_bytes()
        if hashlib.sha256(body).hexdigest() != lock["artifacts"][name]["sha256"]:
            raise ValueError("Installed executor artifact differs from its release lock")


if __name__ == "__main__":
    install()
    verify_installed()
