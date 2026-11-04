"""Image repair cannot turn an unknown or still-running GPU into a cleanup waiver."""

import hashlib
import json
from datetime import UTC, datetime

import host_executor_recovery as recovery
import pytest
from platform_contracts import HostTaskObservation
from platform_management.host_executor.controller import canonical
from test_bootstrap_cleanup import fence
from test_host_executor import task


@pytest.fixture
def chain(tmp_path):
    prepare = task()
    check = task("2", action="cleanup-check", previous_task_id=prepare.id)
    revoke = task("3", action="revoke-bootstrap", previous_task_id=check.id, provider_fence=fence())
    for item in (prepare, check, revoke):
        directory = tmp_path / "executions" / item.id
        directory.mkdir(parents=True)
        sha = hashlib.sha256(canonical(item)).hexdigest()
        good = item.action == "cleanup-check"
        observed = HostTaskObservation(
            task_id=item.id,
            input_sha256=sha,
            job_uid="job-" + item.id,
            observed_at=datetime.now(UTC),
            registry_revision=1,
            status="completed" if good else "failed",
            stage="Synthetic test",
            cleanup_pending=not good,
            bootstrap_fence_permitted=good,
            evidence_sha256="e" * 64 if good else None,
            error=None if good else {"code": "synthetic_failure", "message": "Fixture error"},
        )
        (directory / "task.json").write_bytes(canonical(item))
        (directory / "observation.json").write_text(observed.model_dump_json())
        (directory / "job-uid").write_text(observed.job_uid)
        (directory / "receipt.json").write_text(
            json.dumps(
                {
                    "job_uid": observed.job_uid,
                    "manifest": {"metadata": {"annotations": {"klip.dev/input-sha256": sha}}},
                }
            )
        )
    (tmp_path / "active-task.json").write_text(json.dumps({"id": revoke.id}))
    return tmp_path, revoke


def test_fenced_never_joined_chain_preserves_each_input_and_terminal_owner(chain):
    root, head = chain
    result = recovery.fenced_chain(root, head.id)
    assert result["tasks"] == ["host-" + str(i) * 24 for i in (3, 2, 1)]
    assert len(result["files_sha256"]) == 12
    assert result["provider_fence"]["provider_instance_id"] == head.provider_instance_id
    assert recovery.fenced_chain(root, head.id) == result


def test_upgrade_cannot_count_two_heads_for_one_provider_instance(chain):
    import host_executor_upgrade as upgrade

    root, head = chain
    evidence = recovery.fenced_chain(root, head.id)
    with pytest.raises(ValueError, match="distinct fenced instance"):
        upgrade.guard_program("a" * 64, [evidence, evidence])


@pytest.mark.parametrize(
    "change", ["active-owner", "input-hash", "job-owner", "running", "joined", "identity"]
)
def test_unproven_or_changed_recovery_chain_is_rejected(chain, change):
    root, head = chain
    directory = root / "executions" / head.id
    if change == "active-owner":
        (root / "active-task.json").write_text(json.dumps({"id": "host-" + "4" * 24}))
    elif change == "job-owner":
        (directory / "job-uid").write_text("different-job")
    else:
        path = directory / ("task.json" if change == "identity" else "observation.json")
        value = json.loads(path.read_text())
        if change == "input-hash":
            value["input_sha256"] = "b" * 64
        elif change == "running":
            value.update(status="running", error=None, cleanup_pending=False)
        elif change == "joined":
            value.update(node_name=head.node_name, node_uid="11111111-2222-3333-4444-555555555555")
        else:
            value["private_ip"] = "192.168.10.2"
        path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        recovery.fenced_chain(root, head.id)


@pytest.mark.parametrize(
    "problem", [None, "list-input", "extra-pending", "tampered-bytes", "node-present"]
)
def test_actual_upgrade_guard_requires_exact_fenced_proof_and_absent_node(
    chain, monkeypatch, problem
):
    import time

    import cluster
    import host_executor_upgrade as upgrade
    from host_executor_release import CPU_NAMES

    root, head = chain
    evidence = recovery.fenced_chain(root, head.id)
    (root / "cluster").mkdir()
    (root / "cluster/peers.json").write_text(
        json.dumps(
            {
                "revision": 12,
                "peers": {name: {} for name in CPU_NAMES},
            }
        )
    )
    monkeypatch.setenv("KLIP_UPGRADE_ID", "a" * 64)
    monkeypatch.setenv("POD_UID", "original-guard")
    monkeypatch.setattr(upgrade, "PREFLIGHT_PROGRAM", 'print("{}")')
    monkeypatch.setattr(cluster, "load_hosts", lambda: {"cp-01": {}})

    class ControlPlane:
        def __init__(self, fields):
            pass

        def run(self, args):
            assert args[3:6] == ["get", "node", head.node_name]
            return '{"metadata":{"name":"still-present"}}' if problem == "node-present" else ""

    monkeypatch.setattr(cluster, "Host", ControlPlane)

    def release(_):
        (root / "upgrades" / ("a" * 64) / "release").touch()

    monkeypatch.setattr(time, "sleep", release)
    if problem == "extra-pending":
        extra = root / "executions" / ("host-" + "4" * 24)
        extra.mkdir()
        (extra / "task.json").write_text("{}")
        (extra / "observation.json").write_text('{"status":"failed","cleanup_pending":true}')
    elif problem == "tampered-bytes":
        path = root / "executions" / head.id / "observation.json"
        path.write_bytes(path.read_bytes() + b" ")
    proof = [evidence] if problem == "list-input" else evidence
    program = upgrade.guard_program("a" * 64, proof).replace("/executor/state", str(root))
    if problem not in {None, "list-input"}:
        with pytest.raises(ValueError):
            exec(compile(program, "fenced-upgrade-guard", "exec"), {})
        assert not (root / "upgrades" / ("a" * 64) / "guard.json").exists()
    else:
        exec(compile(program, "fenced-upgrade-guard", "exec"), {})
        assert (root / "upgrades" / ("a" * 64) / "guard.json").exists()
