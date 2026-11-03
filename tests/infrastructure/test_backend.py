import json

import backend
import pytest
from backend import classify_bootstrap, read_state


def state(lineage="one", serial=1, resources=None):
    return {"lineage": lineage, "serial": serial, "resources": resources or []}


def test_first_bootstrap_uses_local_state():
    assert classify_bootstrap(False, None, None) == "new-local"


@pytest.mark.parametrize("bucket_exists", [False, True])
def test_interrupted_local_bootstrap_is_recoverable(bucket_exists):
    assert classify_bootstrap(bucket_exists, state(), None) == "recover-local"


def test_new_checkout_uses_existing_remote_ownership():
    assert classify_bootstrap(True, None, state()) == "remote"


def test_completed_migration_and_older_recovery_copy():
    assert classify_bootstrap(True, state(serial=1), state(serial=2)) == "remote"
    assert classify_bootstrap(True, state(), state()) == "remote"


@pytest.mark.parametrize(
    "local,remote",
    [
        (state("one"), state("two")),
        (state(serial=2), state(serial=1)),
        (state(resources=[{"id": "first"}]), state(resources=[{"id": "second"}])),
    ],
)
def test_conflicting_ownership_is_never_overwritten(local, remote):
    with pytest.raises(ValueError):
        classify_bootstrap(True, local, remote)


def test_bucket_without_state_does_not_get_silently_adopted():
    with pytest.raises(ValueError, match="duplicate ownership"):
        classify_bootstrap(True, None, None)


def test_invalid_recovery_metadata_is_rejected(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"serial":0}')
    with pytest.raises(ValueError, match="Invalid Terraform state"):
        read_state(path)


def test_interrupted_migration_preserves_recovery_and_migrates_once(monkeypatch, tmp_path):
    before = state(serial=7)
    statuses = iter([("recover-local", before, None), ("remote", before, before)])
    calls = []
    monkeypatch.setenv("KLIP_STATE_HOME", str(tmp_path / "operator"))
    monkeypatch.setitem(backend.ROOTS, "bootstrap", tmp_path)
    override = tmp_path / "local_backend_override.tf"
    override.write_text("local backend")
    monkeypatch.setattr(backend.Aws, "preflight", lambda *args: {})
    monkeypatch.setattr(backend, "bootstrap_status", lambda aws: next(statuses))
    monkeypatch.setattr(backend, "tf", lambda *args, **kwargs: calls.append(args) or "")
    backend.migrate_bootstrap()
    assert not override.exists()
    assert len(calls) == 1 and "-migrate-state" in calls[0] and "-force-copy" in calls[0]
    recovery = tmp_path / "operator/bootstrap/recovery-before-migration.json"
    assert json.loads(recovery.read_text()) == before
    assert recovery.stat().st_mode & 0o777 == 0o600


def test_remote_rerun_never_force_copies_local_state(monkeypatch, tmp_path):
    current = state(serial=3)
    calls = []
    monkeypatch.setenv("KLIP_STATE_HOME", str(tmp_path / "operator"))
    monkeypatch.setitem(backend.ROOTS, "bootstrap", tmp_path)
    monkeypatch.setattr(backend.Aws, "preflight", lambda *args: {})
    monkeypatch.setattr(backend, "bootstrap_status", lambda aws: ("remote", current, current))
    monkeypatch.setattr(backend, "tf", lambda *args, **kwargs: calls.append(args) or "")
    backend.migrate_bootstrap()
    assert len(calls) == 1 and "-reconfigure" in calls[0]
    assert "-force-copy" not in calls[0]


def test_conflict_stops_before_backend_or_migration_commands(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(backend.Aws, "preflight", lambda *args: {})

    def conflict(aws):
        return classify_bootstrap(True, state("original"), state("other"))

    monkeypatch.setattr(backend, "bootstrap_status", conflict)
    monkeypatch.setattr(backend, "tf", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="Conflicting"):
        backend.migrate_bootstrap()
    assert calls == []
