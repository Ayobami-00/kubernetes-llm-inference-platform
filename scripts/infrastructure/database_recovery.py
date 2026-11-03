"""Durable, isolated PostgreSQL backup/recovery acceptance; never replace the source."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import time

import yaml
from common import private_dir, source_digest, state_home, utc_now, write_private
from database import NAME, NAMESPACE
from smoke import wait_until

RESTORE = "klip-db-restore"
PLUGIN = "barman-cloud.cloudnative-pg.io"


def operation_paths(operation: str):
    if not re.fullmatch(r"[a-f0-9]{12}", operation):
        raise ValueError("Use a stable 12-character lowercase hexadecimal operation ID")
    folder = private_dir(state_home() / "cluster/database/recovery" / operation)
    return folder, folder / "backup.json"


def resource(kind: str, name: str) -> dict:
    from cluster import kubectl

    raw = kubectl("-n", NAMESPACE, "get", kind, name, "--ignore-not-found", "-o", "json")
    return json.loads(raw) if raw.strip() else {}


def sql(cluster: str, statement: str) -> str:
    from cluster import kubectl

    primary = resource("cluster", cluster)["status"]["currentPrimary"]
    return kubectl(
        "-n",
        NAMESPACE,
        "exec",
        primary,
        "-c",
        "postgres",
        "--",
        "psql",
        "-X",
        "-q",
        "-A",
        "-t",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "postgres",
        "-d",
        "management",
        "-c",
        statement,
    ).strip()


def assert_source(receipt: dict) -> dict:
    current = resource("cluster", NAME)
    if current.get("metadata", {}).get("uid") != receipt["source_cluster_uid"]:
        raise ValueError("Source database identity changed; refusing to reuse the operation")
    return current


def marker_statement(operation: str, cohort: str, payload: str) -> str:
    # Values enter SQL only after strict validation. The markers contain no customer data.
    if (
        not re.fullmatch(r"[a-f0-9]{12}", operation)
        or cohort not in {"base", "wal"}
        or not re.fullmatch(r"[a-f0-9]{64}", payload)
    ):
        raise ValueError("Invalid recovery marker")
    return (
        "CREATE SCHEMA IF NOT EXISTS klip_verification; "
        "CREATE TABLE IF NOT EXISTS klip_verification.recovery_markers "
        "(operation text, cohort text, payload text NOT NULL, "
        "created_at timestamptz NOT NULL DEFAULT clock_timestamp(), "
        "PRIMARY KEY(operation, cohort)); "
        "INSERT INTO klip_verification.recovery_markers(operation,cohort,payload) "
        f"VALUES ('{operation}','{cohort}','{payload}') ON CONFLICT DO NOTHING;"
    )


def assert_backup_owned(backup: dict, receipt: dict) -> None:
    if (
        backup.get("metadata", {}).get("name") != receipt["backup_name"]
        or backup["metadata"].get("labels", {}).get("klip.dev/recovery-operation")
        != receipt["operation"]
        or backup.get("spec", {}).get("cluster", {}).get("name") != NAME
        or backup["spec"].get("method") != "plugin"
        or backup["spec"].get("pluginConfiguration", {}).get("name") != PLUGIN
        or (receipt.get("backup_uid") and backup["metadata"]["uid"] != receipt["backup_uid"])
    ):
        raise ValueError("Backup identity or ownership differs from the durable intent")


def backup(operation: str) -> dict:
    from cluster import Host, load_hosts
    from database import verify

    folder, path = operation_paths(operation)
    if path.exists():
        receipt = json.loads(path.read_text())
        assert_source(receipt)
    else:
        verified = verify()
        receipt = {
            "schema_version": "klip-database-recovery-backup.v1",
            "operation": operation,
            "started_at": utc_now(),
            "source_sha256": source_digest(),
            "source_cluster_uid": verified["cluster_uid"],
            "backup_name": "klip-recovery-" + operation,
            "markers": {
                cohort: hashlib.sha256(f"klip-recovery:{operation}:{cohort}".encode()).hexdigest()
                for cohort in ("base", "wal")
            },
        }
        write_private(path, receipt)
    current = assert_source(receipt)
    if not any(
        c["type"] == "ContinuousArchiving" and c["status"] == "True"
        for c in current.get("status", {}).get("conditions", [])
    ):
        raise ValueError("Continuous WAL archiving is not healthy")
    existing = resource("backup", receipt["backup_name"])
    if not existing:
        if receipt.get("backup_uid"):
            raise ValueError(
                "Recorded backup disappeared; never recreate it under the same operation"
            )
        sql(NAME, marker_statement(operation, "base", receipt["markers"]["base"]))
        document = {
            "apiVersion": "postgresql.cnpg.io/v1",
            "kind": "Backup",
            "metadata": {
                "name": receipt["backup_name"],
                "namespace": NAMESPACE,
                "labels": {"klip.dev/recovery-operation": operation},
            },
            "spec": {
                "cluster": {"name": NAME},
                "method": "plugin",
                "target": "primary",
                "pluginConfiguration": {"name": PLUGIN},
            },
        }
        # Durable intent was saved before this mutation; a repeated invocation reconciles by name.
        Host(load_hosts()["cp-01"]).run(
            ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "create", "-f", "-"],
            data=json.dumps(document),
        )
        existing = resource("backup", receipt["backup_name"])
    assert_backup_owned(existing, receipt)
    receipt["backup_uid"] = existing["metadata"]["uid"]
    write_private(path, receipt)

    def completed():
        observed = resource("backup", receipt["backup_name"])
        assert_backup_owned(observed, receipt)
        phase = observed.get("status", {}).get("phase")
        if phase == "failed":
            write_private(folder / "failed-backup.json", observed)
            raise ValueError("Backup failed; inspect the retained private diagnostic")
        return observed if phase == "completed" else None

    observed = wait_until(completed, "external PostgreSQL base backup", timeout=900, interval=5)
    receipt["backup_status"] = observed["status"]
    write_private(path, receipt)
    if not receipt.get("restore_point"):
        sql(NAME, marker_statement(operation, "wal", receipt["markers"]["wal"]))
        restore_point = "klip_recovery_" + operation
        lsn = sql(NAME, f"SELECT pg_create_restore_point('{restore_point}');")
        segment = sql(NAME, f"SELECT pg_walfile_name('{lsn}');")
        receipt.update(restore_point=restore_point, restore_lsn=lsn, restore_wal=segment)
        write_private(path, receipt)
    if not receipt.get("verified_at"):
        sql(NAME, "SELECT pg_switch_wal();")

        def archived():
            result = json.loads(
                sql(
                    NAME,
                    "SELECT json_build_object("
                    "'last_archived_wal',last_archived_wal,'last_archived_time',last_archived_time,"
                    "'failed_count',failed_count,'last_failed_time',last_failed_time) "
                    "FROM pg_stat_archiver;",
                )
            )
            return (
                result
                if (result.get("last_archived_wal") or "") >= receipt["restore_wal"]
                else None
            )

        receipt["archive_observation"] = wait_until(archived, "marker WAL archiving", timeout=300)
        receipt["verified_at"] = utc_now()
        write_private(path, receipt)
    return receipt


def prepare_restore(operation: str) -> dict:
    from cluster import checked, kubectl
    from common import PROFILES, ROOT, Aws, tool
    from database import assert_empty_archive, values_for_database
    from platform_checks import load_schemas, validate_custom_resources
    from platform_storage import verify_storage

    folder, backup_path = operation_paths(operation)
    receipt = json.loads(backup_path.read_text())
    current = assert_source(receipt)
    observed = resource("backup", receipt["backup_name"])
    assert_backup_owned(observed, receipt)
    backup_id = observed.get("status", {}).get("backupId", "")
    if (
        not receipt.get("verified_at")
        or observed.get("status", {}).get("phase") != "completed"
        or not re.fullmatch(r"[0-9]{8}T[0-9]{6}", backup_id)
        or receipt["backup_status"].get("backupId") != backup_id
    ):
        raise ValueError("The pinned base backup and subsequent WAL must be verified first")
    if resource("cluster", RESTORE):
        raise ValueError(
            "The isolated restore identity already exists; verify or recover its receipt"
        )
    plan_path = folder / "restore.json"
    if plan_path.exists():
        raise ValueError("A restore plan already exists; use its exact retained receipt")
    allocation = verify_storage()["allocation"]
    values = values_for_database(allocation)
    # A new output destination is mandatory, even though recovery reads the source archive.
    destination = values["backup"]["destinationPath"]
    values["backup"]["destinationPath"] = destination.rsplit("/", 1)[0] + "/restore-" + operation
    values["bootstrap"] = {
        "mode": "recovery",
        "recovery": {
            "destinationPath": destination,
            "serverName": NAME,
            "target": {"backupID": backup_id, "targetName": receipt["restore_point"]},
        },
    }
    source_store = resource("objectstore", NAME + "-backups")
    if source_store.get("spec", {}).get("configuration", {}).get("destinationPath") != destination:
        raise ValueError("The source archive no longer matches the retained allocation")
    if current["spec"]["imageName"] != values["imageName"]:
        raise ValueError("Recovery must use the source's verified immutable PostgreSQL image")
    aws = Aws(PROFILES["storage"])
    aws.preflight("storage")
    bucket = allocation["database"]["bucket"]
    prefix = allocation["database"]["archive_prefix"] + "/restore-" + operation + "/"
    assert_empty_archive(aws, bucket, prefix)
    values_path = folder / "restore-values.yaml"
    write_private(values_path, yaml.safe_dump(values))
    rendered = checked(
        [
            tool("helm"),
            "template",
            RESTORE,
            str(ROOT / "charts/klip-database"),
            "--namespace",
            NAMESPACE,
            "--values",
            str(values_path),
        ]
    )
    validate_custom_resources([v for v in yaml.safe_load_all(rendered) if v], load_schemas())
    write_private(folder / "restore-rendered.yaml", rendered)
    plan = {
        "schema_version": "klip-database-isolated-restore.v1",
        "operation": operation,
        "created_at": utc_now(),
        "cluster": RESTORE,
        "source_cluster_uid": receipt["source_cluster_uid"],
        "backup_uid": receipt["backup_uid"],
        "backup_id": backup_id,
        "restore_point": receipt["restore_point"],
        "source_sha256": source_digest(),
        "namespace_uid": json.loads(kubectl("get", "namespace", NAMESPACE, "-o", "json"))[
            "metadata"
        ]["uid"],
        "values_sha256": hashlib.sha256(values_path.read_bytes()).hexdigest(),
        "archive_bucket": bucket,
        "archive_prefix": prefix,
        "resource_envelope": {
            "instances": 3,
            "additional_gp3_gib": 180,
            "monthly_usd_at_reviewed_rate": 16.704,
        },
    }
    plan["approval_sha256"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    write_private(plan_path, plan)
    return plan


def validate_restore_plan(plan: dict, approval: str, values: bytes, source: str) -> None:
    observed = {"approval_sha256", "mutation_started_at", "cluster_uid", "verified_at"}
    frozen = {k: v for k, v in plan.items() if k not in observed}
    if (
        not approval
        or approval != plan.get("approval_sha256")
        or approval != hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()
        or plan.get("cluster") != RESTORE
        or plan.get("source_sha256") != source
        or plan.get("values_sha256") != hashlib.sha256(values).hexdigest()
    ):
        raise ValueError("Restore plan, source or frozen values changed after review")


def restore(operation: str, approval: str) -> dict:
    from cluster import FOLDER, checked, kubeconfigs, kubectl, tunnel
    from common import PROFILES, ROOT, Aws, tool
    from database import assert_empty_archive

    folder, backup_path = operation_paths(operation)
    receipt = json.loads(backup_path.read_text())
    plan_path = folder / "restore.json"
    plan = json.loads(plan_path.read_text())
    values_path = folder / "restore-values.yaml"
    validate_restore_plan(plan, approval, values_path.read_bytes(), source_digest())
    assert_source(receipt)
    assert_backup_owned(resource("backup", receipt["backup_name"]), receipt)
    if (
        plan["backup_uid"] != receipt["backup_uid"]
        or plan["source_cluster_uid"] != receipt["source_cluster_uid"]
    ):
        raise ValueError("Restore and backup receipts identify different source resources")
    namespace = json.loads(kubectl("get", "namespace", NAMESPACE, "-o", "json"))
    if namespace["metadata"]["uid"] != plan["namespace_uid"]:
        raise ValueError("Restore namespace identity changed")
    existing = resource("cluster", RESTORE)
    if existing:
        if (
            not plan.get("mutation_started_at")
            or (
                existing["metadata"].get("annotations", {}).get("meta.helm.sh/release-name")
                != RESTORE
            )
            or (plan.get("cluster_uid") and plan["cluster_uid"] != existing["metadata"]["uid"])
        ):
            raise ValueError("Refusing to adopt or overwrite an unrelated restore cluster")
    else:
        if plan.get("cluster_uid"):
            raise ValueError("Restore cluster disappeared; do not automatically recreate it")
        aws = Aws(PROFILES["storage"])
        aws.preflight("storage")
        assert_empty_archive(aws, plan["archive_bucket"], plan["archive_prefix"])
        plan["mutation_started_at"] = utc_now()
        write_private(plan_path, plan)
        kubeconfigs()
        with tunnel():
            output = checked(
                [
                    tool("helm"),
                    "upgrade",
                    "--install",
                    RESTORE,
                    str(ROOT / "charts/klip-database"),
                    "--namespace",
                    NAMESPACE,
                    "--kubeconfig",
                    str(FOLDER / "operator.conf"),
                    "--values",
                    str(values_path),
                    "--wait",
                    "--timeout",
                    "10m",
                    "--history-max",
                    "10",
                ],
                timeout=750,
            )
        write_private(folder / "restore-helm.log", output)
        existing = resource("cluster", RESTORE)
    plan["cluster_uid"] = existing["metadata"]["uid"]
    write_private(plan_path, plan)
    wait_until(
        lambda: resource("cluster", RESTORE).get("status", {}).get("readyInstances") == 3,
        "isolated three-instance PostgreSQL recovery",
        timeout=1200,
    )
    return verify_restore(operation)


def verify_restore(operation: str) -> dict:
    from database import verify

    started = time.monotonic()
    folder, backup_path = operation_paths(operation)
    receipt = json.loads(backup_path.read_text())
    plan_path = folder / "restore.json"
    plan = json.loads(plan_path.read_text())
    assert_source(receipt)
    current = resource("cluster", RESTORE)
    if not plan.get("cluster_uid") or current.get("metadata", {}).get("uid") != plan["cluster_uid"]:
        raise ValueError("The restored cluster is not the recorded recovery identity")
    observed = verify(RESTORE)
    rows = json.loads(
        sql(
            RESTORE,
            "SELECT json_object_agg(cohort,payload) FROM klip_verification.recovery_markers "
            f"WHERE operation='{operation}';",
        )
    )
    if rows != receipt["markers"]:
        raise ValueError("Recovered base/WAL markers differ from committed source values")
    details = json.loads(
        sql(
            RESTORE,
            "SELECT json_build_object('system_id',system_identifier::text,"
            "'in_recovery',pg_is_in_recovery(),'version',current_setting('server_version')) "
            "FROM pg_control_system();",
        )
    )
    if details["in_recovery"]:
        raise ValueError("Restored database has not finished promotion")
    plan["verified_at"] = utc_now()
    write_private(plan_path, plan)
    result = {
        "schema_version": "klip-database-recovery-verification.v1",
        "operation": operation,
        "verified_at": plan["verified_at"],
        "source_sha256": source_digest(),
        "source_cluster_uid": receipt["source_cluster_uid"],
        "backup_id": plan["backup_id"],
        "restore_point": plan["restore_point"],
        "restore": observed,
        "postgresql": details,
        "base_marker_matches": True,
        "post_backup_wal_marker_matches": True,
        "restore_started_at": plan["mutation_started_at"],
        "verification_seconds": round(time.monotonic() - started, 3),
        "not_covered_by_this_check": [
            "management data migration",
            "application failover",
            "full-environment retained-state rebuild",
        ],
    }
    write_private(folder / "verification.json", result)
    return result


if __name__ == "__main__":
    from cluster import FOLDER

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["backup", "restore-prepare", "restore", "verify"])
    parser.add_argument("--operation", required=True)
    parser.add_argument("--approve-plan")
    args = parser.parse_args()
    private_dir(FOLDER)
    with (FOLDER / "operator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "backup":
            result = backup(args.operation)
        elif args.command == "restore-prepare":
            result = prepare_restore(args.operation)
        elif args.command == "restore":
            result = restore(args.operation, args.approve_plan)
        else:
            result = verify_restore(args.operation)
    print(json.dumps(result, indent=2))
