"""Check the image's real offline dependency verification before paid preparation."""

import hashlib
import io
import json
import tarfile

import gpu_runtime
import host_executor_image
import pytest


@pytest.fixture
def installed(tmp_path, monkeypatch):
    root = tmp_path / "image"
    cache = root / "artifacts/platform"
    cache.mkdir(parents=True)
    (root / "deploy").mkdir()
    lock = {"artifacts": {}}
    for name in host_executor_image.REQUIRED:
        body = ("synthetic-test-" + name).encode()
        (cache / name).write_bytes(body)
        lock["artifacts"][name] = {"sha256": hashlib.sha256(body).hexdigest()}
    (root / "deploy/artifacts.lock.json").write_text(json.dumps(lock))
    for relative in host_executor_image.PUBLIC_HOST_INPUTS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Synthetic build closure fixture\n")
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        item = tarfile.TarInfo("bin/containerd")
        item.size = 4
        archive.addfile(item, io.BytesIO(b"test"))
    payload = stream.getvalue()
    value = gpu_runtime.settings()
    value["containerd_artifact"]["sha256"] = hashlib.sha256(payload).hexdigest()
    (cache / "containerd-gpu-linux-amd64").write_bytes(payload)
    (root / "deploy/gpu-host.lock.json").write_text(json.dumps(value))
    monkeypatch.setattr(host_executor_image, "ROOT", root)
    monkeypatch.setattr(gpu_runtime, "ROOT", root)
    return cache


def test_image_verifies_installed_inputs_without_network_or_host_operations(installed):
    host_executor_image.verify_installed()


@pytest.mark.parametrize("artifact", [*host_executor_image.REQUIRED, "containerd-gpu-linux-amd64"])
def test_image_rejects_corrupt_shipped_tool_before_runtime(installed, artifact):
    (installed / artifact).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum|release lock"):
        host_executor_image.verify_installed()


@pytest.mark.parametrize("relative", host_executor_image.PUBLIC_HOST_INPUTS)
def test_missing_ansible_host_input_fails_the_image_build(installed, relative):
    (host_executor_image.ROOT / relative).unlink()
    with pytest.raises(ValueError, match="shipped host input"):
        host_executor_image.verify_installed()
