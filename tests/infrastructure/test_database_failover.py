"""A primary fault preserves quorum/ownership and cannot silently be repeated."""

import copy
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import cluster
import database_failover as failover
import pytest


def allocation():
    value = {
        "metadata": {"name": "klip-db", "uid": "source-cluster"},
        "spec": {
            "instances": 3,
            "postgresql": {
                "synchronous": {
                    "dataDurability": "required",
                    "method": "any",
                    "number": 1,
                    "failoverQuorum": True,
                }
            },
        },
        "status": {
            "readyInstances": 3,
            "currentPrimary": "klip-db-1",
            "targetPrimary": "klip-db-1",
        },
    }
    pods = [
        {
            "metadata": {
                "name": f"klip-db-{i}",
                "namespace": "platform-data",
                "uid": f"pod-{i}",
                "ownerReferences": [
                    {"kind": "Cluster", "uid": "source-cluster", "controller": True}
                ],
            },
            "spec": {"nodeName": f"platform-0{i}"},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
        for i in (1, 2, 3)
    ]
    return value, pods


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "quorum",
        "durability",
        "count",
        "owner",
        "primary",
        "readiness",
        "namespace",
        "placement",
    ],
)
def test_primary_selection_rejects_unsafe_or_unrelated_targets(fault):
    value, pods = allocation()
    if fault == "quorum":
        value["spec"]["postgresql"]["synchronous"]["failoverQuorum"] = False
    if fault == "durability":
        value["spec"]["postgresql"]["synchronous"]["dataDurability"] = "preferred"
    if fault == "count":
        pods.pop()
    if fault == "owner":
        pods[0]["metadata"]["ownerReferences"][0]["uid"] = "unrelated"
    if fault == "primary":
        value["status"]["currentPrimary"] = "klip-db-4"
    if fault == "readiness":
        pods[1]["status"]["conditions"][0]["status"] = "False"
    if fault == "namespace":
        pods[0]["metadata"]["namespace"] = "another"
    if fault == "placement":
        pods[0]["spec"]["nodeName"] = "cp-01"
    before = copy.deepcopy(pods)
    if fault:
        with pytest.raises(ValueError):
            failover.primary_target(value, pods, ["platform-01", "platform-02", "platform-03"])
    else:
        assert failover.primary_target(
            value, pods, ["platform-01", "platform-02", "platform-03"]
        ) == {"name": "klip-db-1", "uid": "pod-1", "node": "platform-01"}
    assert pods == before


def test_stale_backup_cannot_reach_live_backup_or_fault(tmp_path, monkeypatch):
    path = tmp_path / "backup.json"
    path.write_text(
        json.dumps(
            {
                "source_cluster_uid": "source-cluster",
                "verified_at": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
                "backup_status": {"phase": "completed"},
                "restore_wal": "000002",
                "archive_observation": {"last_archived_wal": "000002"},
            }
        )
    )
    monkeypatch.setattr(failover, "operation_paths", lambda _: (tmp_path, path))
    remote = Mock(side_effect=AssertionError("stale evidence must fail before any remote request"))
    monkeypatch.setattr(failover, "resource", remote)
    with pytest.raises(ValueError, match="fresh completed backup"):
        failover.backup_reference("123456abcdef", "source-cluster")
    remote.assert_not_called()


def test_resuming_submitted_fault_observes_without_deleting_again(tmp_path, monkeypatch):
    monkeypatch.setattr(failover, "folder", lambda: tmp_path)
    operation, backup = "123456abcdef", "abcdef123456"
    root = tmp_path / "failures" / operation
    root.mkdir(parents=True)
    record = {
        "stage": "submitted",
        "backup_operation": backup,
        "cluster_uid": "source-cluster",
        "target": {"name": "klip-db-1"},
        "markers": {"base": "a" * 64, "wal": "b" * 64},
        "targets": ["platform-01", "platform-02", "platform-03"],
        "database_before": {"system_id": "synthetic-system", "timeline": 1},
    }
    (root / "postgresql.json").write_text(json.dumps(record))
    value, pods = allocation()
    value["status"].update(currentPrimary="klip-db-2", targetPrimary="klip-db-2")
    monkeypatch.setattr(failover, "resource", lambda *a: value)
    monkeypatch.setattr(failover, "observe", lambda: (value, pods))
    monkeypatch.setattr(failover, "verify", lambda: {"cluster_uid": "source-cluster"})
    mutate = Mock(side_effect=AssertionError("the original fault must never be repeated"))
    monkeypatch.setattr(cluster, "delete_owned_resource", mutate)

    def sql(_cluster, statement):
        if "pg_control_system" in statement:
            return json.dumps(
                {
                    "system_id": "synthetic-system",
                    "timeline": 2,
                    "in_recovery": False,
                    "synchronous_commit": "on",
                    "synchronous_standby_names": 'ANY 1 ("klip-db-1", "klip-db-3")',
                }
            )
        if "json_object_agg" in statement:
            return json.dumps(record["markers"])
        if "pg_stat_archiver" in statement:
            return json.dumps({"wal": "000002"})
        return "000001"

    monkeypatch.setattr(failover, "sql", sql)
    result = failover.run(operation, backup)
    assert result["stage"] == "recovered" and result["pre_fault_marker_preserved"]
    assert result["database_write_recovery_seconds"] is None
    assert result["database_write_objective_met"] is None  # No invented timer across interruption.
    assert failover.run(operation, backup) == result
    with pytest.raises(ValueError, match="different backup"):
        failover.run(operation, "000000000000")
    mutate.assert_not_called()
