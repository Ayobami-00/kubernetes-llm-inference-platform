"""Operator recovery-key custody, backup freshness and isolated restore validation."""

from __future__ import annotations

import hashlib
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from common import CONFIG, PROFILES, Aws, state_home, tool, utc_now, write_private


def validate_bundle(path: Path, expected_instance: str) -> dict:
    with tarfile.open(path) as archive:
        for entry in archive.getmembers():
            if (
                entry.name.startswith("/")
                or ".." in Path(entry.name).parts
                or entry.issym()
                or entry.islnk()
            ):
                raise ValueError("Unsafe recovery archive entry")
        names = set(archive.getnames())
        if (
            not {"snapshot.db", "metadata.json", "encryption.yaml", "pki/ca.crt", "pki/ca.key"}
            <= names
        ):
            raise ValueError("Incomplete recovery bundle")
        metadata = json.load(archive.extractfile("metadata.json"))
        if (
            metadata["schema_version"] != "klip-recovery.v1"
            or metadata["cluster_id"] != "klip"
            or metadata["instance_id"] != expected_instance
        ):
            raise ValueError("Recovery bundle belongs to a different cluster or instance")
        with archive.extractfile("snapshot.db") as snapshot:
            digest = hashlib.file_digest(snapshot, "sha256").hexdigest()
        if digest != metadata["snapshot_sha256"]:
            raise ValueError("Snapshot checksum mismatch")
        return metadata


def key_custody(folder: Path) -> dict:
    identity = folder / "backup-identity.txt"
    receipt = json.loads((folder / "key-custody.json").read_text())
    if not receipt.get("independent_custody_confirmed"):
        raise ValueError("Confirm that an independent recovery-key copy is retained")
    if hashlib.sha256(identity.read_bytes()).hexdigest() != receipt["sha256"]:
        raise ValueError("Recovery identity changed after copy verification")
    copy = Path(receipt["copy_path"])
    if copy.exists() and hashlib.sha256(copy.read_bytes()).hexdigest() != receipt["sha256"]:
        raise ValueError("The independently retained recovery-key copy is unavailable")
    return receipt


def backup_fresh(receipt: dict, now: datetime | None = None) -> None:
    age = (
        (now or datetime.now(UTC)) - datetime.fromisoformat(receipt["captured_at"])
    ).total_seconds()
    if not receipt.get("version_id") or age < -60 or age > 86400:
        raise ValueError("Backup is unversioned or older than the 24-hour recovery objective")


def restore_on_probe(archive_path: Path, host, image: str) -> dict:

    if not image.startswith("registry.k8s.io/etcd:") or "latest" in image:
        raise ValueError("Restore requires the live cluster's pinned etcd image")
    if host.fields["klip_node"]["role"] != "probe":
        raise ValueError("Isolated restore cannot run on a cluster member")
    with tarfile.open(archive_path) as archive:
        import base64

        snapshot = base64.b64encode(archive.extractfile("snapshot.db").read()).decode()
    folder = "/var/lib/klip/restore-test"
    # Never copy CA private keys or the operator's decryption identity to the probe.
    host.run(["sudo", "install", "-d", "-m", "0700", folder])
    host.run(
        [
            "sudo",
            "python3",
            "-c",
            "import base64,sys; from pathlib import Path; "
            "Path(sys.argv[1]).write_bytes(base64.b64decode(sys.stdin.read()))",
            folder + "/snapshot.db",
        ],
        data=snapshot,
        secret=True,
    )
    mount = f"type=bind,src={folder},dst=/restore,options=rbind:rw"
    try:
        host.run(["sudo", "ctr", "images", "pull", image], timeout=300)
        host.run(
            [
                "sudo",
                "ctr",
                "run",
                "--rm",
                "--mount",
                mount,
                image,
                "klip-restore-prepare",
                "etcdutl",
                "snapshot",
                "restore",
                "/restore/snapshot.db",
                "--data-dir=/restore/data",
                "--name=restore",
                "--initial-cluster=restore=http://127.0.0.1:12380",
                "--initial-advertise-peer-urls=http://127.0.0.1:12380",
                "--bump-revision=1000000000",
                "--mark-compacted",
            ],
            timeout=300,
        )
        host.run(
            [
                "sudo",
                "ctr",
                "run",
                "--detach",
                "--net-host",
                "--mount",
                mount,
                image,
                "klip-restore-check",
                "etcd",
                "--data-dir=/restore/data",
                "--name=restore",
                "--listen-client-urls=http://127.0.0.1:12379",
                "--advertise-client-urls=http://127.0.0.1:12379",
                "--listen-peer-urls=http://127.0.0.1:12380",
                "--initial-advertise-peer-urls=http://127.0.0.1:12380",
            ]
        )
        import time

        for attempt in range(20):
            try:
                host.run(
                    [
                        "sudo",
                        "ctr",
                        "tasks",
                        "exec",
                        "--exec-id",
                        "health",
                        "klip-restore-check",
                        "etcdctl",
                        "--endpoints=http://127.0.0.1:12379",
                        "endpoint",
                        "health",
                    ]
                )
                break
            except RuntimeError:
                if attempt == 19:
                    raise
                time.sleep(1)
        keys = host.run(
            [
                "sudo",
                "ctr",
                "tasks",
                "exec",
                "--exec-id",
                "read",
                "klip-restore-check",
                "etcdctl",
                "--endpoints=http://127.0.0.1:12379",
                "get",
                "/registry/",
                "--prefix",
                "--keys-only",
                "--limit=1",
            ]
        )
        if "/registry/" not in keys:
            raise ValueError("Restored snapshot contains no Kubernetes registry records")
        return {"isolated_restore": True, "full_cluster_disaster_recovery": False}
    finally:
        cleanup_errors = []
        try:
            tasks = host.run(["sudo", "ctr", "tasks", "list", "--quiet"]).splitlines()
            if "klip-restore-check" in tasks:
                host.run(
                    ["sudo", "ctr", "tasks", "kill", "--signal", "SIGTERM", "klip-restore-check"]
                )
                host.run(["sudo", "ctr", "tasks", "delete", "--force", "klip-restore-check"])
            containers = host.run(["sudo", "ctr", "containers", "list", "--quiet"]).splitlines()
            if "klip-restore-check" in containers:
                host.run(["sudo", "ctr", "containers", "delete", "klip-restore-check"])
        except (RuntimeError, OSError) as error:
            cleanup_errors.append(str(error))
        try:
            host.run(["sudo", "rm", "-rf", folder])
        except (RuntimeError, OSError) as error:
            cleanup_errors.append(str(error))
        if cleanup_errors:
            write_private(state_home() / "cluster/restore-cleanup-errors.json", cleanup_errors)
            raise RuntimeError(
                "Isolated restore cleanup failed; inspect the private cleanup report"
            )


def operate_backup(
    action: str, copy: Path | None = None, *, independent_custody: bool = False
) -> dict:
    from cluster import FOLDER, Host, ansible, checked, load_hosts, preflight

    identity = FOLDER / "backup-identity.txt"
    recipient_file = FOLDER / "backup-recipient.txt"
    if action == "key-init":
        if not identity.exists():
            if recipient_file.exists() or (FOLDER / "key-custody.json").exists():
                raise ValueError(
                    "Recover the existing age identity; silent replacement is forbidden"
                )
            checked([tool("age-keygen"), "-o", str(identity)], secret=True)
            identity.chmod(0o600)
        recipient = checked([tool("age-keygen"), "-y", str(identity)], secret=True).strip()
        write_private(recipient_file, recipient)
        return {
            "recipient": recipient,
            "identity_path": str(identity),
            "independent_copy_required": True,
        }
    if action == "key-verify-copy":
        if (
            not copy
            or copy.resolve() == identity.resolve()
            or copy.resolve().is_relative_to(state_home().resolve())
        ):
            raise ValueError(
                "Supply the independently retained copy outside the operator state directory"
            )
        if (
            hashlib.sha256(copy.read_bytes()).digest()
            != hashlib.sha256(identity.read_bytes()).digest()
        ):
            raise ValueError("Recovery key copies differ")
        receipt = {
            "verified_at": utc_now(),
            "copy_path": str(copy.resolve()),
            "sha256": hashlib.sha256(identity.read_bytes()).hexdigest(),
            "independent_custody_confirmed": independent_custody,
        }
        write_private(FOLDER / "key-custody.json", receipt)
        return {"copy_verified": True}
    key_custody(FOLDER)
    if action == "backup":
        preflight()
        ansible("backup", extra={"klip_recovery_key_verified": True})
        results = {
            name: Host(fields).agent("backup")
            for name, fields in load_hosts().items()
            if fields["klip_node"]["role"] == "control_plane"
        }
        write_private(FOLDER / "backups.json", results)
        return {"backed_up": sorted(results)}
    results = {}
    aws = Aws(PROFILES["foundation"])
    aws.preflight("foundation")
    for name, fields in load_hosts().items():
        if fields["klip_node"]["role"] != "control_plane":
            continue
        Host(fields).run(["sudo", "systemctl", "is-active", "klip-etcd-backup.timer"])
        Host(fields).run(["sudo", "systemctl", "is-enabled", "klip-etcd-backup.timer"])
        receipt = Host(fields).agent("status")["backup"]
        if not receipt:
            raise ValueError(f"No successful backup observed for {name}")
        backup_fresh(receipt)
        encrypted = FOLDER / f"backup-{name}.age"
        write_private(encrypted, "")
        aws.call(
            "s3api",
            "get-object",
            "--bucket",
            fields["klip_node"]["backup_bucket"],
            "--key",
            receipt["key"],
            "--version-id",
            receipt["version_id"],
            "--expected-bucket-owner",
            CONFIG["account_id"],
            str(encrypted),
        )
        if hashlib.sha256(encrypted.read_bytes()).hexdigest() != receipt["sha256"]:
            raise ValueError("Downloaded encrypted backup checksum differs")
        archive = FOLDER / f"backup-{name}.tar.gz"
        write_private(archive, "")
        try:
            checked(
                [tool("age"), "--decrypt", "-i", str(identity), "-o", str(archive), str(encrypted)],
                secret=True,
            )
            metadata = validate_bundle(archive, fields["klip_node"]["instance_id"])
            results[name] = metadata
            if name == "cp-01" and "network-probe" in load_hosts():
                from cluster import kubectl

                image = json.loads(
                    kubectl("-n", "kube-system", "get", "pod", "etcd-cp-01", "-o", "json")
                )["spec"]["containers"][0]["image"]
                results[name]["restore"] = restore_on_probe(
                    archive, Host(load_hosts()["network-probe"]), image
                )
        finally:
            archive.unlink(missing_ok=True)
    write_private(
        FOLDER / "backup-verification.json", {"captured_at": utc_now(), "members": results}
    )
    return {"backup_checks": results}
