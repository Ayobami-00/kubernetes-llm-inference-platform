"""Copy the preserved local management database into an empty HA application schema."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import socket
import subprocess
from contextlib import contextmanager

import psycopg
import yaml
from common import ROOT, private_dir, source_digest, state_home, tool, utc_now, write_private
from database import NAME
from database_recovery import resource, sql

CONTAINER = "inference-management-db-1"
MAX_DUMP_BYTES = 128 * 1024 * 1024


def paths():
    folder = private_dir(state_home() / "cluster/database/local-migration")
    return folder, folder / "receipt.json", folder / "management.dump"


def fingerprint_query(table: str, exclude_columns: tuple[str, ...] = ()) -> str:
    if any(not re.fullmatch(r"[a-z_][a-z0-9_]*", name) for name in (table, *exclude_columns)):
        raise ValueError("Unexpected management table identifier")
    # Row ordering is independent of heap layout. SHA256 protects the dump itself;
    # these MD5 fingerprints diagnose logical row differences across PostgreSQL versions.
    record = "to_jsonb(t)"
    if exclude_columns:
        record += " - ARRAY[" + ",".join("'" + name + "'" for name in exclude_columns) + "]"
    return (
        "SELECT json_build_object('count',count(*),'content_fingerprint',"
        f"md5(coalesce(string_agg(md5(({record})::text),'' "
        f"ORDER BY md5(({record})::text)),''))) FROM public.\"{table}\" t;"
    )


def tables_query() -> str:
    return (
        "SELECT coalesce(json_agg(tablename ORDER BY tablename),'[]') "
        "FROM pg_tables WHERE schemaname='public';"
    )


def prepare() -> dict:
    folder, receipt_path, dump = paths()
    if receipt_path.exists():
        raise ValueError("A migration receipt already exists; inspect/reuse it, never overwrite it")
    if json.loads(sql(NAME, tables_query())):
        raise ValueError(
            "The target application schema is not empty; refusing migration preparation"
        )
    compose = yaml.safe_load((ROOT / "dev/compose.yaml").read_text())
    service = compose["services"]["db"]
    inspect = json.loads(
        subprocess.check_output(
            [
                "docker",
                "inspect",
                "--format",
                '{"id":{{json .Id}},"image":{{json .Config.Image}},'
                '"ports":{{json .NetworkSettings.Ports}}}',
                CONTAINER,
            ],
            text=True,
        )
    )
    if inspect["image"] != service["image"] or inspect["ports"].get("5432/tcp") != [
        {"HostIp": "127.0.0.1", "HostPort": "55432"}
    ]:
        raise ValueError(
            "Local database image or loopback binding differs from the preserved source"
        )
    connection = psycopg.connect(
        host="127.0.0.1",
        port=55432,
        dbname="management",
        user=service["environment"]["POSTGRES_USER"],
        password=service["environment"]["POSTGRES_PASSWORD"],
    )
    connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    connection.read_only = True
    with connection as db:
        db.execute("SET TIME ZONE 'UTC'")
        others = db.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE "
            "datname=current_database() AND pid<>pg_backend_pid() "
            "AND backend_type='client backend'"
        ).fetchone()[0]
        if others:
            raise ValueError("Stop local application clients before taking the migration snapshot")
        snapshot = db.execute("SELECT pg_export_snapshot()").fetchone()[0]
        tables = db.execute(tables_query()).fetchone()[0]
        if not tables or "alembic_version" not in tables:
            raise ValueError("Local source does not contain a management application schema")
        fingerprints = {name: db.execute(fingerprint_query(name)).fetchone()[0] for name in tables}
        fd = os.open(dump, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                result = subprocess.run(
                    [
                        "docker",
                        "exec",
                        inspect["id"],
                        "pg_dump",
                        "-U",
                        "management",
                        "-d",
                        "management",
                        "-Fc",
                        "--no-owner",
                        "--no-privileges",
                        "--schema=public",
                        "--snapshot",
                        snapshot,
                    ],
                    stdout=stream,
                    stderr=subprocess.PIPE,
                    timeout=180,
                    check=False,
                )
            if result.returncode:
                raise RuntimeError("Local pg_dump failed; secret-bearing diagnostic withheld")
            if dump.stat().st_size > MAX_DUMP_BYTES:
                raise ValueError(
                    "Development migration exceeds 128 MiB; use a reviewed streaming path"
                )
        except BaseException:
            dump.unlink(missing_ok=True)
            raise
        version = db.execute("SELECT current_setting('server_version')").fetchone()[0]
        schema_version = db.execute("SELECT version_num FROM public.alembic_version").fetchone()[0]
    receipt = {
        "schema_version": "klip-local-management-migration.v1",
        "created_at": utc_now(),
        "source_sha256": source_digest(),
        "source_container_id": inspect["id"],
        "source_image": inspect["image"],
        "source_postgresql_version": version,
        "source_schema_version": schema_version,
        "source_snapshot": snapshot,
        "target_cluster_uid": resource("cluster", NAME)["metadata"]["uid"],
        "dump_bytes": dump.stat().st_size,
        "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
        "tables": fingerprints,
        "source_retained": True,
    }
    receipt["approval_sha256"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True).encode()
    ).hexdigest()
    write_private(receipt_path, receipt)
    return receipt


def validate_receipt(receipt: dict, approval: str, dump: bytes) -> None:
    observed = {"approval_sha256", "mutation_started_at", "verified_at"}
    frozen = {k: v for k, v in receipt.items() if k not in observed}
    if (
        not approval
        or approval != receipt.get("approval_sha256")
        or approval != hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()
        or hashlib.sha256(dump).hexdigest() != receipt["dump_sha256"]
    ):
        raise ValueError("The migration receipt or retained dump changed after review")


def restore(approval: str) -> dict:
    from cluster import Host, load_hosts

    _, path, dump_path = paths()
    receipt = json.loads(path.read_text())
    data = dump_path.read_bytes()
    validate_receipt(receipt, approval, data)
    current = resource("cluster", NAME)
    if current["metadata"]["uid"] != receipt["target_cluster_uid"]:
        raise ValueError("Migration target database identity changed")
    existing = json.loads(sql(NAME, tables_query()))
    if existing:
        if not receipt.get("mutation_started_at"):
            raise ValueError(
                "Target acquired application data after review; refusing to overwrite it"
            )
        return verify()  # Reconcile a committed restore whose local receipt was interrupted.
    receipt["mutation_started_at"] = utc_now()
    write_private(path, receipt)
    program = r"""
import base64, subprocess, sys
data=base64.b64decode(sys.stdin.buffer.read(),validate=True)
if len(data)>128*1024*1024:
    raise ValueError('Migration dump exceeds its bound')
result=subprocess.run([
 'kubectl','--kubeconfig=/etc/kubernetes/admin.conf','-n','platform-data','exec','-i',
 sys.argv[1],'-c','postgres','--','pg_restore','-U','postgres','-d','management',
 '--role=management','--no-owner','--no-privileges','--clean','--if-exists','--single-transaction'
],input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=300)
if result.returncode:
    raise RuntimeError('Transactional pg_restore failed; secret-bearing output withheld')
"""
    Host(load_hosts()["cp-01"]).run(
        [
            "sudo",
            "python3",
            "-c",
            program,
            current["status"]["currentPrimary"],
        ],
        data=base64.b64encode(data).decode(),
        secret=True,
        timeout=360,
    )
    return verify()


def verify() -> dict:
    _, path, _ = paths()
    receipt = json.loads(path.read_text())
    if resource("cluster", NAME)["metadata"]["uid"] != receipt["target_cluster_uid"]:
        raise ValueError("Migration verification target changed")
    names = json.loads(sql(NAME, tables_query()))
    if sorted(names) != sorted(receipt["tables"]):
        raise ValueError("Restored table identities differ from the frozen source")
    for name, expected in receipt["tables"].items():
        actual = json.loads(sql(NAME, "SET TIME ZONE 'UTC'; " + fingerprint_query(name)))
        if actual != expected:
            raise ValueError("Restored rows differ for table " + name)
    receipt["verified_at"] = utc_now()
    write_private(path, receipt)
    # Do not print row content, credentials, login/session state or the dump itself.
    return {
        "verified_at": receipt["verified_at"],
        "target_cluster_uid": receipt["target_cluster_uid"],
        "tables_matching": len(names),
        "rows_matching": sum(x["count"] for x in receipt["tables"].values()),
        "source_retained": True,
        "schema_upgrade_pending": True,
    }


@contextmanager
def database_tunnel(folder):
    from cluster import FOLDER, kubeconfigs, tunnel
    from common import PROFILES, environment
    from smoke import wait_until

    with socket.socket() as probe:
        probe.bind(
            ("127.0.0.1", 15432)
        )  # Refuse an occupied port rather than connecting elsewhere.
    kubeconfigs()
    log = folder / "database-forward.log"
    write_private(log, "")
    with tunnel(), log.open("a") as output:
        process = subprocess.Popen(
            [
                tool("kubectl"),
                "--kubeconfig",
                str(FOLDER / "operator.conf"),
                "-n",
                "platform-data",
                "port-forward",
                "--address=127.0.0.1",
                "service/klip-db-rw",
                "15432:5432",
            ],
            stdout=output,
            stderr=output,
            env=environment(PROFILES["foundation"]),
        )

        def listening():
            if process.poll() is not None:
                raise ValueError("The database forwarding process exited; inspect its private log")
            # A raw TCP canary can reset the PostgreSQL connection and terminate
            # kubectl's forwarding session. Its bound-listener log is the startup signal.
            return "Forwarding from 127.0.0.1:15432 -> 5432" in log.read_text()

        try:
            wait_until(listening, "private database forwarding", timeout=30, interval=1)
            yield
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def upgrade() -> dict:
    from cluster import kubectl
    from sqlalchemy.engine import URL

    folder, receipt_path, _ = paths()
    receipt = json.loads(receipt_path.read_text())
    if not receipt.get("verified_at") or receipt.get("source_schema_version") != "0001":
        raise ValueError("This upgrade requires a verified revision-0001 local data migration")
    if resource("cluster", NAME)["metadata"]["uid"] != receipt["target_cluster_uid"]:
        raise ValueError("The schema upgrade target identity changed")
    secret = json.loads((state_home() / "credentials/management-postgresql.json").read_text())
    # Retrieve only the public CA key, never the Secret's private signing key.
    ca = base64.b64decode(
        kubectl(
            "-n", "platform-data", "get", "secret", "klip-db-ca", "-o", r"jsonpath={.data.ca\.crt}"
        )
    )
    ca_path = folder / "ca.crt"
    write_private(ca_path, ca.decode())
    url = URL.create(
        "postgresql+psycopg",
        username="management",
        password=secret["password"],
        host="klip-db-rw.platform-data.svc",
        port=15432,
        database="management",
        query={"hostaddr": "127.0.0.1", "sslmode": "verify-full"},
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MANAGEMENT_", "PG", "AWS_"))}
    env.update(
        MANAGEMENT_DATABASE_URL=url.render_as_string(hide_password=False),
        MANAGEMENT_DATABASE_REQUIRE_TLS="true",
        MANAGEMENT_DATABASE_CA_FILE=str(ca_path),
    )
    probe = """
import json
from alembic import command
from alembic.config import Config
from platform_management.db.session import engine
from sqlalchemy import text
database=engine()
try:
 with database.connect() as c:
  row=c.execute(text("SELECT current_user AS role, s.ssl, s.version, s.cipher, "
                    "r.rolsuper FROM pg_stat_ssl s JOIN pg_roles r "
                    "ON r.rolname=current_user WHERE s.pid=pg_backend_pid()")).mappings().one()
  if not row['ssl'] or row['rolsuper']:
   raise ValueError('Application TLS/role policy is not satisfied')
  config=Config('apps/management/backend/alembic.ini')
  config.attributes['connection']=c
  command.upgrade(config,'head')
  print(json.dumps(dict(row)))
finally:
 database.dispose()
"""
    with database_tunnel(folder):
        result = subprocess.run(
            [str(ROOT / ".venv/bin/python"), "-c", probe],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if secret["password"] in result.stdout + result.stderr:
            raise RuntimeError("Schema upgrade diagnostic contained a credential; output withheld")
        write_private(folder / "schema-upgrade.log", result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError("Schema upgrade failed; inspect its private diagnostic")
        tls = json.loads(result.stdout)
    version = sql(NAME, "SELECT version_num FROM public.alembic_version;")
    if version != "0002":
        raise ValueError("Unexpected final management schema revision")
    added = {
        "resources": ("binding_id",),
        "runs": ("binding_id",),
        "jobs": ("binding_id", "operation_id", "kind"),
    }
    for name, expected in receipt["tables"].items():
        if name == "alembic_version":
            continue
        observed = json.loads(
            sql(NAME, "SET TIME ZONE 'UTC'; " + fingerprint_query(name, added.get(name, ())))
        )
        if observed != expected:
            raise ValueError(
                "Original row content changed unexpectedly during schema upgrade: " + name
            )
    output = {
        "schema_version": "klip-management-schema-upgrade.v1",
        "verified_at": utc_now(),
        "source_sha256": source_digest(),
        "target_cluster_uid": receipt["target_cluster_uid"],
        "from_revision": "0001",
        "to_revision": version,
        "application_connection": tls,
        "original_data_preserved": True,
        "source_database_retained": True,
        "management_replicas_deployed": False,
    }
    write_private(folder / "schema-upgrade.json", output)
    return output


if __name__ == "__main__":
    from cluster import FOLDER

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "restore", "verify", "upgrade"])
    parser.add_argument("--approve-plan")
    args = parser.parse_args()
    with (FOLDER / "operator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = (
            prepare()
            if args.command == "prepare"
            else restore(args.approve_plan)
            if args.command == "restore"
            else upgrade()
            if args.command == "upgrade"
            else verify()
        )
    print(json.dumps(result, indent=2))
