"""Encrypted off-cluster custody of the management database credential."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json

from cluster_backups import key_custody
from common import CONFIG, PROFILES, Aws, state_home, tf, tool, utc_now, write_private


def validate_payload(value: dict) -> dict:
    if (
        value.get("schema_version") != "klip-database-secret.v1"
        or value.get("cluster_id") != CONFIG["cluster_id"]
        or value.get("account_id") != CONFIG["account_id"]
        or value.get("credential_reference") != "credential://klip/management-postgresql"
    ):
        raise ValueError("Recovery secret bundle belongs to another platform identity")
    credential = value.get("credential", {})
    if (
        credential.get("username") != "management"
        or credential.get("namespace") != "platform-data"
        or credential.get("cluster_id") != CONFIG["cluster_id"]
        or not isinstance(credential.get("password"), str)
        or len(credential["password"]) < 32
    ):
        raise ValueError("Recovery secret bundle has invalid credential metadata")
    return credential


def operate(action: str) -> dict:
    from cluster import FOLDER, checked

    custody = key_custody(FOLDER)
    aws = Aws(PROFILES["storage"])
    aws.preflight("storage")
    folder = state_home() / "cluster/database"
    credential_path = state_home() / "credentials/management-postgresql.json"
    receipt_path = folder / "secret-custody.json"
    encrypted_path = folder / "management-postgresql.age"
    if action == "backup" and not receipt_path.exists():
        credential = json.loads(credential_path.read_text())
        payload = {
            "schema_version": "klip-database-secret.v1",
            "created_at": utc_now(),
            "account_id": CONFIG["account_id"],
            "cluster_id": CONFIG["cluster_id"],
            "credential_reference": "credential://klip/management-postgresql",
            "credential": credential,
        }
        validate_payload(payload)
        recipient = checked(
            [tool("age-keygen"), "-y", str(FOLDER / "backup-identity.txt")], secret=True
        ).strip()
        encrypted = checked(
            [tool("age"), "--armor", "--recipient", recipient],
            data=json.dumps(payload),
            secret=True,
        )
        write_private(encrypted_path, encrypted)
        allocation = json.loads(tf("storage", "output", "-json", "storage_manifest"))
        bucket = allocation["database"]["bucket"]
        stamp = payload["created_at"].replace(":", "").replace("+", "_")
        key = "recovery-secrets/management-postgresql-" + stamp + ".age"
        receipt = {
            "schema_version": "klip-database-secret-custody.v1",
            "bucket": bucket,
            "key": key,
            "sha256": hashlib.sha256(encrypted_path.read_bytes()).hexdigest(),
            "created_at": payload["created_at"],
            "recovery_identity_sha256": custody["sha256"],
        }
        # Persist the exact destination/checksum before an ambiguous upload can occur.
        write_private(receipt_path, receipt)
    receipt = json.loads(receipt_path.read_text())
    if receipt["recovery_identity_sha256"] != custody["sha256"]:
        raise ValueError("The secret recovery key differs from its independently verified copy")
    allocation = json.loads(tf("storage", "output", "-json", "storage_manifest"))
    if (
        receipt["bucket"] != allocation["database"]["bucket"]
        or not receipt["key"].startswith("recovery-secrets/management-postgresql-")
        or not receipt["key"].endswith(".age")
    ):
        raise ValueError("Unexpected recovery secret destination")
    if not receipt.get("version_id"):
        if action != "backup":
            raise ValueError("Complete or reconcile the pending encrypted upload first")
        if hashlib.sha256(encrypted_path.read_bytes()).hexdigest() != receipt["sha256"]:
            raise ValueError("Pending encrypted payload differs from the durable upload intent")
        uploaded = aws.call(
            "s3api",
            "head-object",
            "--bucket",
            receipt["bucket"],
            "--key",
            receipt["key"],
            "--expected-bucket-owner",
            CONFIG["account_id"],
            missing=True,
        )
        if uploaded:
            if uploaded.get("Metadata", {}).get("sha256") != receipt["sha256"]:
                raise ValueError("An unrelated object occupies the reserved encrypted secret key")
        else:
            uploaded = aws.call(
                "s3api",
                "put-object",
                "--bucket",
                receipt["bucket"],
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
            raise ValueError("Secret recovery upload is not versioned")
        receipt["version_id"] = uploaded["VersionId"]
        write_private(receipt_path, receipt)
    downloaded = folder / "management-postgresql-download.age"
    write_private(downloaded, "")
    try:
        metadata = aws.call(
            "s3api",
            "get-object",
            "--bucket",
            receipt["bucket"],
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
            raise ValueError("Encrypted recovery object differs from the custody receipt")
        plaintext = checked(
            [
                tool("age"),
                "--decrypt",
                "--identity",
                str(FOLDER / "backup-identity.txt"),
                str(downloaded),
            ],
            secret=True,
        )
        recovered = validate_payload(json.loads(plaintext))
        if action == "restore" and not credential_path.exists():
            write_private(credential_path, recovered)
        actual = json.loads(credential_path.read_text())
        if not hmac.compare_digest(
            json.dumps(recovered, sort_keys=True), json.dumps(actual, sort_keys=True)
        ):
            raise ValueError("Recovered credential differs from the retained local identity")
    finally:
        downloaded.unlink(missing_ok=True)
    receipt.update(verified_at=utc_now(), decrypted_credential_matches=True)
    write_private(receipt_path, receipt)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["backup", "verify", "restore"])
    args = parser.parse_args()
    from cluster import FOLDER

    with (FOLDER / "operator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = operate(args.command)
    print(json.dumps(result, indent=2))
