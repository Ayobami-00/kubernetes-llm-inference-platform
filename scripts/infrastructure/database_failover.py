"""Isolated, UID-bound primary Pod loss with committed markers and automatic promotion.

Do not force promotion or weaken synchronous replication. An independent caller
keeps its pre-fault authenticated session and measures public application recovery.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import time
from datetime import UTC, datetime, timedelta

from common import private_dir, source_digest, utc_now, write_private
from database import NAME, NAMESPACE, verify
from database_recovery import assert_backup_owned, marker_statement, operation_paths, resource, sql
from image_credentials import healthy_targets
from management_release import folder
from smoke import wait_until


def primary_target(value, pods, targets):
    synchronous = value["spec"]["postgresql"].get("synchronous", {})
    if (
        value["metadata"]["name"] != NAME
        or value["spec"]["instances"] != 3
        or value["status"].get("readyInstances") != 3
        or synchronous
        != {"dataDurability": "required", "method": "any", "number": 1, "failoverQuorum": True}
        or len(pods) != 3
        or len({p["spec"]["nodeName"] for p in pods}) != 3
    ):
        raise ValueError(
            "Primary fault requires three spread replicas with the frozen quorum policy"
        )
    for p in pods:
        if (
            p["metadata"].get("namespace") != NAMESPACE
            or p["spec"]["nodeName"] not in targets
            or p["metadata"].get("deletionTimestamp")
            or not any(
                o.get("controller")
                and o["kind"] == "Cluster"
                and o["uid"] == value["metadata"]["uid"]
                for o in p["metadata"].get("ownerReferences", [])
            )
            or not any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in p.get("status", {}).get("conditions", [])
            )
        ):
            raise ValueError("Database Pod ownership or readiness differs")
    selected = next(
        (p for p in pods if p["metadata"]["name"] == value["status"]["currentPrimary"]), None
    )
    if not selected or not re.fullmatch(r"klip-db-[0-9]+", selected["metadata"]["name"]):
        raise ValueError("The current primary is not an expected database Pod")
    return {
        "name": selected["metadata"]["name"],
        "uid": selected["metadata"]["uid"],
        "node": selected["spec"]["nodeName"],
    }


def observe():
    from cluster import kubectl

    return resource("cluster", NAME), json.loads(
        kubectl(
            "-n",
            NAMESPACE,
            "get",
            "pods",
            "-l",
            "cnpg.io/cluster=klip-db,cnpg.io/podRole=instance",
            "-o",
            "json",
        )
    )["items"]


def database_state():
    result = json.loads(
        sql(
            NAME,
            "SELECT json_build_object('system_id',s.system_identifier::text,"
            "'timeline',c.timeline_id,'in_recovery',pg_is_in_recovery(),"
            "'synchronous_commit',current_setting('synchronous_commit'),"
            "'synchronous_standby_names',current_setting('synchronous_standby_names')) "
            "FROM pg_control_system() s CROSS JOIN pg_control_checkpoint() c;",
        )
    )
    if (
        result["in_recovery"]
        or result["synchronous_commit"] not in {"on", "remote_apply"}
        or not result["synchronous_standby_names"]
    ):
        raise ValueError("The observed primary is not using synchronous durable commits")
    return result


def backup_reference(operation, cluster_uid):
    _, path = operation_paths(operation)
    receipt = json.loads(path.read_text())
    verified = datetime.fromisoformat(receipt.get("verified_at", ""))
    if (
        receipt["source_cluster_uid"] != cluster_uid
        or verified.tzinfo is None
        or not datetime.now(UTC) - timedelta(hours=24) <= verified <= datetime.now(UTC)
        or receipt.get("backup_status", {}).get("phase") != "completed"
        or receipt.get("archive_observation", {}).get("last_archived_wal", "")
        < receipt.get("restore_wal", "~")
    ):
        raise ValueError("A fresh completed backup with verified WAL is required")
    current = resource("backup", receipt["backup_name"])
    assert_backup_owned(current, receipt)
    if current.get("status", {}).get("phase") != "completed":
        raise ValueError("The live backup is not completed")
    return {
        "operation": operation,
        "backup_uid": receipt["backup_uid"],
        "backup_id": receipt["backup_status"]["backupId"],
        "receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def run(operation, backup_operation):
    from cluster import delete_owned_resource

    if not re.fullmatch(r"[a-f0-9]{12}", operation):
        raise ValueError("Use a stable 12-character lowercase hexadecimal operation")
    root = private_dir(folder() / "failures" / operation)
    path = root / "postgresql.json"
    with (root / "operation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fault_start = None
        if path.exists():
            record = json.loads(path.read_text())
            if record["backup_operation"] != backup_operation:
                raise ValueError("Existing fault operation has different backup inputs")
            if record["stage"] == "recovered":
                return record
        else:
            targets = healthy_targets()
            verified = verify()
            backup = backup_reference(backup_operation, verified["cluster_uid"])
            value, pods = observe()
            target = primary_target(value, pods, targets)
            effective = database_state()
            markers = {
                c: hashlib.sha256(f"postgresql-failover:{operation}:{c}".encode()).hexdigest()
                for c in ("base", "wal")
            }
            sql(NAME, marker_statement(operation, "base", markers["base"]))
            record = {
                "operation": operation,
                "backup_operation": backup_operation,
                "backup": backup,
                "stage": "prepared",
                "cluster_uid": verified["cluster_uid"],
                "target": target,
                "targets": targets,
                "markers": markers,
                "pre_fault_commit_acknowledged_at": utc_now(),
                "source_sha256": source_digest(),
                "quorum_policy": value["spec"]["postgresql"]["synchronous"],
                "database_before": effective,
            }
            write_private(path, record)
        if record["stage"] == "prepared":
            value, pods = observe()
            if (
                value["metadata"]["uid"] != record["cluster_uid"]
                or primary_target(value, pods, record["targets"]) != record["target"]
            ):
                raise ValueError("Database primary identity changed before fault submission")
            record.update(stage="submitted", intervention_at=utc_now())
            write_private(path, record)
            fault_start = time.monotonic()
            delete_owned_resource(
                f"/api/v1/namespaces/{NAMESPACE}/pods/{record['target']['name']}",
                record["target"]["uid"],
            )
        # An interrupted submission only observes the original fault. It never
        # deletes a replacement or issues an unsafe manual promotion.
        started = time.monotonic()

        def promoted():
            current = resource("cluster", NAME)
            if current["metadata"]["uid"] != record["cluster_uid"]:
                raise ValueError("Database Cluster identity changed during failover")
            primary = current.get("status", {}).get("currentPrimary")
            return (
                primary
                if primary
                and primary != record["target"]["name"]
                and current["status"].get("targetPrimary") == primary
                else None
            )

        record["promoted_primary"] = wait_until(
            promoted, "automatic promotion of a different primary", timeout=120, interval=1
        )
        record["promotion_observed_at"] = utc_now()
        write_private(path, record)

        def committed():
            sql(NAME, marker_statement(operation, "wal", record["markers"]["wal"]))
            rows = json.loads(
                sql(
                    NAME,
                    "SELECT json_object_agg(cohort,payload) "
                    "FROM klip_verification.recovery_markers "
                    f"WHERE operation='{operation}';",
                )
            )
            if rows != record["markers"]:
                raise ValueError(
                    "The acknowledged pre-fault marker or new-primary write is missing"
                )
            return True

        wait_until(
            committed, "committed new-primary write and preserved marker", timeout=120, interval=1
        )
        elapsed = round(time.monotonic() - fault_start, 3) if fault_start is not None else None
        record.update(
            pre_fault_marker_preserved=True,
            post_failover_commit_acknowledged_at=utc_now(),
            database_write_recovery_seconds=elapsed,
            database_write_objective_met=elapsed <= 120 if elapsed is not None else None,
        )
        record["database_after"] = database_state()
        if (
            record["database_after"]["system_id"] != record["database_before"]["system_id"]
            or record["database_after"]["timeline"] <= record["database_before"]["timeline"]
        ):
            raise ValueError("Promotion did not preserve database identity on a new timeline")
        write_private(path, record)

        def restored():
            value, pods = observe()
            if value["metadata"]["uid"] != record["cluster_uid"]:
                raise ValueError("Database Cluster identity changed during recovery")
            try:
                primary_target(value, pods, record["targets"])
            except ValueError:
                return None
            return verify()

        record["recovered_database"] = wait_until(
            restored, "three-instance recovery after promotion", timeout=240, interval=2
        )
        segment = sql(NAME, "SELECT pg_walfile_name(pg_current_wal_lsn());")
        sql(NAME, "SELECT pg_switch_wal();")

        def archived():
            result = json.loads(
                sql(
                    NAME,
                    "SELECT json_build_object('wal',last_archived_wal,'time',last_archived_time,"
                    "'failed_count',failed_count) FROM pg_stat_archiver;",
                )
            )
            return result if (result.get("wal") or "") >= segment else None

        record["post_failover_archive"] = wait_until(
            archived, "new-primary WAL archive", timeout=120, interval=2
        )
        record.update(
            stage="recovered",
            completed_at=utc_now(),
            observation_wait_seconds=round(time.monotonic() - started, 3),
        )
        write_private(path, record)
        return record


if __name__ == "__main__":
    from cluster import FOLDER

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--backup-operation", required=True)
    args = parser.parse_args()
    with (FOLDER / "operator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(json.dumps(run(args.operation, args.backup_operation)))
