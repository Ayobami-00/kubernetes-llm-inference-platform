import hashlib
import json

import pytest
from platform_contracts import Run, RunnerObservation
from workload_runner import managed
from workload_runner.artifacts import atomic_json
from workload_runner.control import load_run, persist_run, utcnow


def handoff(snapshot, directory):
    directory.mkdir()
    content = json.dumps(snapshot.model_dump(mode="json"), sort_keys=True).encode()
    (directory / "snapshot.json").write_bytes(content)
    manifest = dict(
        schema_version="management-runner.v1",
        run_id="run-management-test",
        attempt_id="op-managed-test",
        snapshot_path=str(directory / "snapshot.json"),
        snapshot_sha256=hashlib.sha256(content).hexdigest(),
        binding_sha256="b" * 64,
        fixture=True,
        deadline_seconds=120,
        argv=["workload-runner", "execute"],
    )
    atomic_json(directory / "manifest.json", manifest)
    return directory / "manifest.json"


def fail_once(directory):
    data = load_run(directory).model_dump(mode="json")
    data.update(status="failed", finished_at=utcnow(), error=dict(code="test", message="test"))
    return persist_run(directory, data)


def test_managed_identity_bytes_and_terminal_replay(compile_scenario, tmp_path, monkeypatch):
    path = handoff(compile_scenario(), tmp_path / "managed")
    before = path.with_name("snapshot.json").read_bytes()
    calls = []

    def execute(directory):
        calls.append(directory)
        return fail_once(directory)

    monkeypatch.setattr(managed, "execute", execute)
    run = managed.execute_manifest(path)
    assert run.id == "run-management-test" and run.status == "failed"
    assert managed.execute_manifest(path) == run
    assert len(calls) == 1
    assert path.with_name("snapshot.json").read_bytes() == before
    observation = RunnerObservation.model_validate_json(
        path.with_name("observation.json").read_text()
    )
    assert observation.status == "failed" and observation.error.code == "test"
    assert (
        json.loads(path.with_name("execution-identity.json").read_text())["attempt_id"]
        == "op-managed-test"
    )


@pytest.mark.parametrize(
    "change", ["checksum", "fixture", "version", "snapshot-path", "attempt-id"]
)
def test_bad_manifests_fail_before_execution(change, compile_scenario, tmp_path, monkeypatch):
    path = handoff(compile_scenario(), tmp_path / "managed")
    data = json.loads(path.read_text())
    key, value = {
        "checksum": ("snapshot_sha256", "0" * 64),
        "fixture": ("fixture", False),
        "version": ("schema_version", "unknown"),
        "snapshot-path": ("snapshot_path", str(tmp_path / "another.json")),
        "attempt-id": ("attempt_id", "../escape"),
    }[change]
    if change == "snapshot-path":
        (tmp_path / "another.json").write_bytes(path.with_name("snapshot.json").read_bytes())
    data[key] = value
    atomic_json(path, data)
    monkeypatch.setattr(managed, "execute", lambda _: pytest.fail("execution must not start"))
    with pytest.raises(ValueError):
        managed.execute_manifest(path)
    assert not path.with_name("run.json").exists()


def test_lost_managed_attempt_never_restarts(compile_scenario, tmp_path, monkeypatch):
    path = handoff(compile_scenario(), tmp_path / "managed")
    monkeypatch.setattr(managed, "execute", lambda d: load_run(d))
    managed.execute_manifest(path)
    with pytest.raises(ValueError, match="restart is forbidden"):
        managed.execute_manifest(path)


def test_secret_file_mapping_is_runtime_only(compile_scenario, tmp_path, monkeypatch):
    snapshot = compile_scenario()
    snapshot.target.credential_env = {snapshot.customers[0].id: "INFERENCE_TOKEN"}
    root = tmp_path / "secrets"
    root.mkdir()
    secret = root / "token"
    secret.write_text("private-inference-secret")
    monkeypatch.setattr(managed, "SECRET_ROOT", root)
    monkeypatch.setattr(managed, "execute", fail_once)
    path = handoff(snapshot, tmp_path / "managed")
    managed.execute_manifest(path, [f"INFERENCE_TOKEN={secret}"])
    import os

    assert "INFERENCE_TOKEN" not in os.environ
    assert all(
        b"private-inference-secret" not in p.read_bytes()
        for p in path.parent.iterdir()
        if p.is_file()
    )
    Run.model_validate_json(path.with_name("run.json").read_text())


def test_redaction_never_rewrites_frozen_inputs(compile_scenario, tmp_path, monkeypatch):
    from workload_runner.control import preflight
    from workload_runner.security import scrub_artifacts

    snapshot = compile_scenario()
    snapshot.target.credential_env = {snapshot.customers[0].id: "INFERENCE_TOKEN"}
    snapshot.derivation["test-marker"] = "private-inference-secret"
    monkeypatch.setenv("INFERENCE_TOKEN", "private-inference-secret")
    path = handoff(snapshot, tmp_path / "managed")
    before = path.with_name("snapshot.json").read_bytes()
    path.with_name("engine.log").write_text("Authorization: private-inference-secret")
    with pytest.raises(ValueError, match="frozen inputs contain"):
        preflight(snapshot)
    scrub_artifacts(path.parent, snapshot)
    assert path.with_name("snapshot.json").read_bytes() == before
    assert path.with_name("engine.log").read_text() == "Authorization: <redacted>"
