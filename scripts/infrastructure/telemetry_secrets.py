"""Encrypted, version-bound off-cluster recovery of Grafana credentials and encryption key."""

from __future__ import annotations

import hashlib
import hmac
import json

from cluster_backups import key_custody
from common import CONFIG, PROFILES, Aws, state_home, tf, tool, utc_now, write_private


def validate(value):
    fields = {"admin-user", "admin-password", "database-password", "encryption-key"}
    credential = value.get("credential", {})
    if (
        value.get("schema_version") != "klip-grafana-secret.v1"
        or value.get("cluster_id") != CONFIG["cluster_id"]
        or value.get("account_id") != CONFIG["account_id"]
        or set(credential) != fields
        or credential["admin-user"] != "admin"
        or any(
            not isinstance(credential[k], str) or len(credential[k]) < 32
            for k in fields - {"admin-user"}
        )
    ):
        raise ValueError("Invalid Grafana recovery identity or credential structure")
    return credential


def operate(action):
    from cluster import FOLDER, checked

    custody = key_custody(FOLDER)
    aws = Aws(PROFILES["storage"])
    aws.preflight("storage")
    folder = state_home() / "cluster/observability/grafana"
    path = state_home() / "credentials/grafana.json"
    receipt_path = folder / "secret-custody.json"
    encrypted_path = folder / "credentials.age"
    allocation = json.loads(tf("storage", "output", "-json", "storage_manifest"))
    bucket = allocation["database"]["bucket"]
    if not receipt_path.exists():
        if action != "backup":
            raise ValueError("A version-bound Grafana recovery receipt is required")
        payload = {
            "schema_version": "klip-grafana-secret.v1",
            "created_at": utc_now(),
            "account_id": CONFIG["account_id"],
            "cluster_id": CONFIG["cluster_id"],
            "credential": json.loads(path.read_text()),
        }
        validate(payload)
        recipient = checked(
            [tool("age-keygen"), "-y", str(FOLDER / "backup-identity.txt")], secret=True
        ).strip()
        encrypted = checked(
            [tool("age"), "--armor", "--recipient", recipient],
            data=json.dumps(payload),
            secret=True,
        )
        write_private(encrypted_path, encrypted)
        stamp = payload["created_at"].replace(":", "").replace("+", "_")
        receipt = {
            "schema_version": "klip-grafana-secret-custody.v1",
            "bucket": bucket,
            "key": "recovery-secrets/grafana-" + stamp + ".age",
            "sha256": hashlib.sha256(encrypted_path.read_bytes()).hexdigest(),
            "created_at": payload["created_at"],
            "recovery_identity_sha256": custody["sha256"],
        }
        write_private(receipt_path, receipt)
    receipt = json.loads(receipt_path.read_text())
    if (
        receipt["bucket"] != bucket
        or not receipt["key"].startswith("recovery-secrets/grafana-")
        or not receipt["key"].endswith(".age")
        or receipt["recovery_identity_sha256"] != custody["sha256"]
    ):
        raise ValueError("Unexpected Grafana recovery destination or key custody")
    if not receipt.get("version_id"):
        if (
            action != "backup"
            or hashlib.sha256(encrypted_path.read_bytes()).hexdigest() != receipt["sha256"]
        ):
            raise ValueError("Reconcile the exact pending encrypted upload first")
        uploaded = aws.call(
            "s3api",
            "head-object",
            "--bucket",
            bucket,
            "--key",
            receipt["key"],
            "--expected-bucket-owner",
            CONFIG["account_id"],
            missing=True,
        )
        if uploaded:
            if uploaded.get("Metadata", {}).get("sha256") != receipt["sha256"]:
                raise ValueError("Unrelated recovery object occupies the reserved identity")
        else:
            uploaded = aws.call(
                "s3api",
                "put-object",
                "--bucket",
                bucket,
                "--key",
                receipt["key"],
                "--body",
                str(encrypted_path),
                "--server-side-encryption",
                "AES256",
                "--if-none-match",
                "*",
                "--metadata",
                json.dumps({"sha256": receipt["sha256"]}),
                "--expected-bucket-owner",
                CONFIG["account_id"],
            )
        if not uploaded.get("VersionId"):
            raise ValueError("Encrypted recovery is not versioned")
        receipt["version_id"] = uploaded["VersionId"]
        write_private(receipt_path, receipt)
    downloaded = folder / "download.age"
    write_private(downloaded, "")
    try:
        metadata = aws.call(
            "s3api",
            "get-object",
            "--bucket",
            bucket,
            "--key",
            receipt["key"],
            "--version-id",
            receipt["version_id"],
            "--expected-bucket-owner",
            CONFIG["account_id"],
            str(downloaded),
        )
        if (
            metadata.get("ServerSideEncryption") != "AES256"
            or hashlib.sha256(downloaded.read_bytes()).hexdigest() != receipt["sha256"]
        ):
            raise ValueError("Recovery ciphertext differs from its receipt")
        recovered = validate(
            json.loads(
                checked(
                    [
                        tool("age"),
                        "--decrypt",
                        "--identity",
                        str(FOLDER / "backup-identity.txt"),
                        str(downloaded),
                    ],
                    secret=True,
                )
            )
        )
        if action == "restore" and not path.exists():
            write_private(path, recovered)
        if not hmac.compare_digest(
            json.dumps(recovered, sort_keys=True),
            json.dumps(json.loads(path.read_text()), sort_keys=True),
        ):
            raise ValueError("Recovered credentials differ; never overwrite a live identity")
    finally:
        downloaded.unlink(missing_ok=True)
    receipt.update(verified_at=utc_now(), decrypted_credential_matches=True)
    write_private(receipt_path, receipt)
    return receipt


def stage_local(action="stage-local"):
    """Keep recovery on the operator machine; this path makes no cloud/network call."""
    from cluster import FOLDER, checked

    custody = key_custody(FOLDER)
    folder = state_home() / "cluster/observability/grafana"
    path = state_home() / "credentials/grafana.json"
    receipt_path = folder / "local-secret-custody.json"
    encrypted_path = folder / "local-credentials.age"
    if not receipt_path.exists():
        if action != "stage-local":
            raise ValueError("A retained local credential receipt is required")
        payload = {
            "schema_version": "klip-grafana-secret.v1",
            "account_id": CONFIG["account_id"],
            "cluster_id": CONFIG["cluster_id"],
            "created_at": utc_now(),
            "credential": json.loads(path.read_text()),
        }
        validate(payload)
        recipient = checked(
            [tool("age-keygen"), "-y", str(FOLDER / "backup-identity.txt")], secret=True
        ).strip()
        encrypted = checked(
            [tool("age"), "--armor", "--recipient", recipient],
            data=json.dumps(payload),
            secret=True,
        )
        write_private(encrypted_path, encrypted)
        receipt = {
            "schema_version": "klip-grafana-local-secret-custody.v1",
            "account_id": CONFIG["account_id"],
            "cluster_id": CONFIG["cluster_id"],
            "created_at": payload["created_at"],
            "sha256": hashlib.sha256(encrypted_path.read_bytes()).hexdigest(),
            "recovery_identity_sha256": custody["sha256"],
            "backup_scope": "operator-local-only",
            "cloud_upload": "not-performed; separate explicit approval required",
            "limitations": (
                "No independent remote credential copy; operator disk loss is not covered"
            ),
        }
        write_private(receipt_path, receipt)
    receipt = json.loads(receipt_path.read_text())
    if (
        receipt["account_id"] != CONFIG["account_id"]
        or receipt["cluster_id"] != CONFIG["cluster_id"]
        or receipt["recovery_identity_sha256"] != custody["sha256"]
        or hashlib.sha256(encrypted_path.read_bytes()).hexdigest() != receipt["sha256"]
    ):
        raise ValueError("Local credential recovery identity or ciphertext changed")
    recovered = validate(
        json.loads(
            checked(
                [
                    tool("age"),
                    "--decrypt",
                    "--identity",
                    str(FOLDER / "backup-identity.txt"),
                    str(encrypted_path),
                ],
                secret=True,
            )
        )
    )
    if action == "restore-local" and not path.exists():
        write_private(path, recovered)
    if not hmac.compare_digest(
        json.dumps(recovered, sort_keys=True),
        json.dumps(json.loads(path.read_text()), sort_keys=True),
    ):
        raise ValueError(
            "Recovered credentials differ; local recovery never overwrites an identity"
        )
    receipt.update(verified_at=utc_now(), decrypted_credential_matches=True)
    write_private(receipt_path, receipt)
    return receipt


def prepare_remote():
    """Stage the exact locally verified ciphertext for review, without any cloud call."""
    local = stage_local("verify-local")
    folder = state_home() / "cluster/observability/grafana"
    ciphertext = (folder / "local-credentials.age").read_text()
    stamp = local["created_at"].replace(":", "").replace("+", "_")
    receipt = {
        "schema_version": "klip-grafana-secret-custody.v1",
        "bucket": f"klip-database-{CONFIG['account_id']}-{CONFIG['region']}",
        "key": "recovery-secrets/grafana-" + stamp + ".age",
        "sha256": local["sha256"],
        "created_at": local["created_at"],
        "recovery_identity_sha256": local["recovery_identity_sha256"],
    }
    path, encrypted = folder / "secret-custody.json", folder / "credentials.age"
    if path.exists():
        previous = json.loads(path.read_text())
        if any(previous.get(k) != v for k, v in receipt.items()):
            raise ValueError("A different remote recovery intent already exists")
        receipt = previous
    if encrypted.exists() and encrypted.read_text() != ciphertext:
        raise ValueError("Pending remote ciphertext differs from verified local recovery")
    if not encrypted.exists():
        write_private(encrypted, ciphertext)
    if not path.exists():
        write_private(path, receipt)
    return receipt


if __name__ == "__main__":
    import argparse
    import fcntl

    from cluster import FOLDER

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "backup",
            "verify",
            "restore",
            "stage-local",
            "verify-local",
            "restore-local",
            "prepare-remote",
        ],
    )
    args = parser.parse_args()
    with (FOLDER / "operator.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = (
            prepare_remote()
            if args.command == "prepare-remote"
            else stage_local(args.command)
            if args.command.endswith("-local")
            else operate(args.command)
        )
        print(json.dumps(result, indent=2))
