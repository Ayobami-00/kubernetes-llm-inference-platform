"""Stage the checksum-pinned GPU host runtime; never change a running CPU host."""

import hashlib
import json
import os
import re
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

from common import ROOT

KERNEL_PROGRAM = r"""
import json,platform,subprocess,sys
from pathlib import Path
expected=json.load(sys.stdin)
packages={}
for name in ('image_package','headers_package','modules_package'):
    package=expected[name]
    packages[package]=subprocess.check_output(
        ['dpkg-query','-W','-f=${db:Status-Status} ${Version}',package],text=True).strip()
release=platform.release()
config=Path('/boot/config-'+release).read_text()
modules=Path('/proc/modules').read_text().splitlines()
print(json.dumps({'release':release,'packages':packages,
    'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
    'nouveau_loaded':any(line.startswith('nouveau ') for line in modules),
    'gcov_instrumentation': 'CONFIG_GCOV_KERNEL=y' in config,
    'gcov_disabled_explicitly': '# CONFIG_GCOV_KERNEL is not set' in config}))
"""


def settings():
    value = json.loads((ROOT / "deploy/gpu-host.lock.json").read_text())
    if (
        value["schema_version"] != "klip-gpu-host.v1"
        or value["runtime_profile"] != "nvidia-operator"
        or value["containerd_version"] != "2.3.7"
        or value["os"] != "ubuntu-24.04"
        or value["architecture"] != "amd64"
    ):
        raise ValueError("Unsupported GPU host runtime lock")
    artifact = value["containerd_artifact"]
    expected = (
        "https://github.com/containerd/containerd/releases/download/v2.3.7/"
        "containerd-2.3.7-linux-amd64.tar.gz"
    )
    if artifact["url"] != expected or artifact["filename"] != "containerd-gpu-linux-amd64":
        raise ValueError("GPU runtime artifact source changed")
    if artifact["max_bytes"] != 128 * 1024 * 1024 or len(artifact["sha256"]) != 64:
        raise ValueError("GPU runtime artifact bound/checksum is invalid")
    if value["kernel"] != {
        "release": "6.8.0-147-generic",
        "package_version": "6.8.0-147.147",
        "image_package": "linux-image-6.8.0-147-generic",
        "headers_package": "linux-headers-6.8.0-147-generic",
        "modules_package": "linux-modules-6.8.0-147-generic",
        "evidence": "https://packages.ubuntu.com/noble-updates/linux-image-6.8.0-147-generic",
    }:
        raise ValueError("GPU kernel requires an explicitly reviewed preparation profile")
    return value


def checked_kernel(value):
    expected = settings()["kernel"]
    if (
        value.get("release") != expected["release"]
        or value.get("packages")
        != {
            expected[key]: "installed " + expected["package_version"]
            for key in ("image_package", "headers_package", "modules_package")
        }
        or value.get("nouveau_loaded") is not False
        or value.get("gcov_instrumentation") is not False
        or value.get("gcov_disabled_explicitly") is not True
        or not isinstance(value.get("boot_id"), str)
        or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", value["boot_id"])
    ):
        raise ValueError(
            "GPU host kernel, installed packages or module/configuration proof differs"
        )
    return value


def observe_kernel(host):
    value = json.loads(
        host.run(
            ["sudo", "python3", "-c", KERNEL_PROGRAM],
            data=json.dumps(settings()["kernel"]),
        )
    )
    return checked_kernel(value)


def verify(path, artifact):
    if path.stat().st_size > artifact["max_bytes"]:
        raise ValueError("GPU runtime archive exceeded its bound")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != artifact["sha256"]:
        raise ValueError("GPU runtime archive checksum differs from the reviewed lock")
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        if not any(m.name == "bin/containerd" and m.isfile() for m in members):
            raise ValueError("GPU runtime archive has no expected binary")
        if len(members) > 100 or sum(m.size for m in members) > 512 * 1024 * 1024:
            raise ValueError("GPU runtime archive exceeds unpacking bounds")
        for member in members:
            name = PurePosixPath(member.name)
            if (
                name.is_absolute()
                or ".." in name.parts
                or not name.parts
                or name.parts[0] != "bin"
                or not (member.isdir() or member.isfile())
            ):
                raise ValueError("GPU runtime archive contains an unsafe path or entry")


def install_artifact(*, opener=urllib.request.urlopen):
    value = settings()
    artifact = value["containerd_artifact"]
    root = ROOT / "artifacts/platform"
    root.mkdir(parents=True, exist_ok=True)
    destination = root / artifact["filename"]
    if destination.exists():
        verify(destination, artifact)
        return destination
    descriptor, temporary = tempfile.mkstemp(prefix=".gpu-containerd-", dir=root)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            request = urllib.request.Request(
                artifact["url"], headers={"User-Agent": "klip-infrastructure/1"}
            )
            with opener(request, timeout=60) as response:
                total = 0
                while block := response.read(65536):
                    total += len(block)
                    if total > artifact["max_bytes"]:
                        raise ValueError("GPU runtime download exceeded its bound")
                    stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        verify(Path(temporary), artifact)
        # A concurrent staging process cannot replace different retained content.
        try:
            os.link(temporary, destination)
        except FileExistsError:
            verify(destination, artifact)
        return destination
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    path = install_artifact()
    print(json.dumps({"artifact": str(path), "version": settings()["containerd_version"]}))
