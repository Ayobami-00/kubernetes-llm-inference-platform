"""GPU runtime selection is explicit and cannot replace existing CPU pins."""

import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime, timedelta

import gpu_runtime
import pytest
from cluster_models import GPUWorkerRequest, WorkerRequest, worker_request
from platform_artifacts import VERSIONS


def request():
    return {
        "schema_version": "klip-worker-operation.v1",
        "operation_id": "gpu-prepare-proof",
        "deadline": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "provider": "lambda",
        "provider_instance_id": "synthetic-lambda-instance",
        "node_name": "gpu-01",
        "public_ip": "8.8.8.8",
        "private_ip": "192.168.2.10",
        "wireguard_ip": "10.200.0.67",
        "access_ref": "credential://klip/gpu-01",
        "registry_revision": 1,
        "ssh_source_cidrs": ["1.1.1.1/32"],
    }


def test_cpu_and_legacy_requests_retain_their_runtime_and_serialization():
    before = WorkerRequest.model_validate(request())
    parsed = worker_request(before.model_dump_json())
    assert type(parsed) is WorkerRequest and parsed.model_dump_json() == before.model_dump_json()
    assert "runtime_profile" not in parsed.model_dump()
    assert VERSIONS["containerd"] == "2.4.2"
    assert gpu_runtime.settings()["containerd_version"] == "2.3.7"


def test_gpu_profile_requires_v2_lambda_and_bootstrap_quarantine():
    document = {
        **request(),
        "schema_version": "klip-worker-operation.v2",
        "runtime_profile": "nvidia-operator",
        "quarantine": True,
    }
    parsed = worker_request(json.dumps(document))
    assert isinstance(parsed, GPUWorkerRequest)
    assert parsed.runtime_profile == "nvidia-operator" and parsed.quarantine is True
    for change in ({"provider": "aws"}, {"quarantine": False}, {"runtime_profile": "standard"}):
        with pytest.raises(ValueError):
            worker_request(json.dumps({**document, **change}))
    with pytest.raises(ValueError):
        worker_request(json.dumps({**request(), "runtime_profile": "nvidia-operator"}))


def archive_bytes(name="bin/containerd", *, kind=tarfile.REGTYPE):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.size = 4 if kind == tarfile.REGTYPE else 0
        archive.addfile(member, io.BytesIO(b"test") if member.size else None)
    return stream.getvalue()


def artifact(payload):
    return {"sha256": hashlib.sha256(payload).hexdigest(), "max_bytes": 128 * 1024 * 1024}


def test_corrupt_cached_runtime_is_not_overwritten_or_extracted(tmp_path, monkeypatch):
    payload = archive_bytes()
    value = gpu_runtime.settings()
    value["containerd_artifact"]["sha256"] = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(gpu_runtime, "ROOT", tmp_path)
    monkeypatch.setattr(gpu_runtime, "settings", lambda: value)
    root = tmp_path / "artifacts/platform"
    root.mkdir(parents=True)
    path = root / "containerd-gpu-linux-amd64"
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        gpu_runtime.install_artifact(opener=lambda *_a, **_kw: pytest.fail("Must not fetch"))
    assert path.read_bytes() == b"corrupt"


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../../etc/host", tarfile.REGTYPE),
        ("/bin/containerd", tarfile.REGTYPE),
        ("bin/containerd", tarfile.SYMTYPE),
        ("another/containerd", tarfile.REGTYPE),
    ],
)
def test_verified_hash_still_requires_safe_expected_archive_contents(tmp_path, name, kind):
    payload = archive_bytes(name, kind=kind)
    path = tmp_path / "runtime.tar.gz"
    path.write_bytes(payload)
    with pytest.raises(ValueError):
        gpu_runtime.verify(path, artifact(payload))


def test_atomic_download_validates_before_install_and_reuses_verified_cache(tmp_path, monkeypatch):
    payload = archive_bytes()
    value = gpu_runtime.settings()
    value["containerd_artifact"]["sha256"] = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(gpu_runtime, "ROOT", tmp_path)
    monkeypatch.setattr(gpu_runtime, "settings", lambda: value)
    calls = []

    def opener(request, **_):
        calls.append(request.full_url)
        return io.BytesIO(payload)

    path = gpu_runtime.install_artifact(opener=opener)
    assert path.read_bytes() == payload
    assert gpu_runtime.install_artifact(opener=opener) == path
    assert len(calls) == 1 and not list(path.parent.glob(".gpu-containerd-*"))


def test_failed_download_cleans_partial_file(tmp_path, monkeypatch):
    value = gpu_runtime.settings()
    value["containerd_artifact"]["max_bytes"] = 3
    monkeypatch.setattr(gpu_runtime, "ROOT", tmp_path)
    monkeypatch.setattr(gpu_runtime, "settings", lambda: value)
    with pytest.raises(ValueError, match="bound"):
        gpu_runtime.install_artifact(opener=lambda *_a, **_kw: io.BytesIO(b"oversized"))
    assert not list((tmp_path / "artifacts/platform").iterdir())


def test_actual_ansible_templates_render_for_cpu_and_gpu_runtime_profiles(monkeypatch):
    pytest.importorskip("ansible")
    import cluster_checks
    from common import ROOT

    monkeypatch.setenv("ANSIBLE_CONFIG", str(ROOT / "infra/ansible/ansible.cfg"))
    monkeypatch.setenv("ANSIBLE_HOME", str(ROOT / "artifacts/ansible"))
    cluster_checks.render_role_templates()
