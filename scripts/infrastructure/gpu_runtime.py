"""Stage the checksum-pinned GPU host runtime; never change a running CPU host."""

import hashlib
import json
import os
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

from common import ROOT


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
    return value


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
