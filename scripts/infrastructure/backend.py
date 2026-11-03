"""Recoverable local-to-S3 bootstrap. No state is discarded or blindly overwritten."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from common import (
    CONFIG,
    PROFILES,
    ROOTS,
    STATE_KEYS,
    Aws,
    private_dir,
    state_home,
    tf,
    utc_now,
    write_private,
)


def read_state(path: Path) -> dict | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text())
    if not value.get("lineage") or not isinstance(value.get("serial"), int):
        raise ValueError(f"Invalid Terraform state metadata: {path}")
    return value


def state_fingerprint(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def verify_migration_metadata_only(local: dict, remote: dict, expected_lineage: str) -> None:
    """Explicit recovery only: never use content equality to bypass normal lineage checks."""
    if local["lineage"] != expected_lineage:
        raise ValueError("Recovery source lineage differs from the explicitly reviewed lineage")
    ignored = {"lineage", "serial", "terraform_version"}
    source = {key: value for key, value in local.items() if key not in ignored}
    destination = {key: value for key, value in remote.items() if key not in ignored}
    if source != destination or not local.get("resources"):
        raise ValueError("Migration payload differs; metadata-only repair is forbidden")


def repair_migration_metadata(expected_version: str, expected_lineage: str) -> None:
    """Repair a reviewed metadata-only migration under native lock and conditional write."""
    if not expected_version or not expected_lineage:
        raise ValueError("Explicit remote version and original local lineage are required")
    aws = Aws(PROFILES["bootstrap"])
    identity = aws.preflight("bootstrap")
    folder = private_dir(state_home() / "bootstrap")
    source = read_state(folder / "recovery-before-migration.json")
    local = read_state(folder / "terraform.tfstate")
    if source is None or local != source:
        raise ValueError("Local state no longer matches the retained pre-migration recovery copy")
    bucket, key, owner = CONFIG["state_bucket"], STATE_KEYS["bootstrap"], CONFIG["account_id"]
    lock_id = str(uuid.uuid4())
    lock = folder / "migration-repair-lock.json"
    write_private(
        lock,
        {
            "ID": lock_id,
            "Operation": "OperationTypeMigrationMetadataRepair",
            "Who": identity["Arn"],
            "Version": "1.17.1",
            "Created": utc_now(),
            "Path": f"{bucket}/{key}",
            "Info": "Reviewed identical-payload migration metadata repair",
        },
    )
    aws.call(
        "s3api",
        "put-object",
        "--bucket",
        bucket,
        "--key",
        key + ".tflock",
        "--body",
        str(lock),
        "--if-none-match",
        "*",
        "--expected-bucket-owner",
        owner,
    )
    try:
        destination = folder / "remote-before-metadata-repair.json"
        write_private(destination, "")
        metadata = aws.call(
            "s3api",
            "get-object",
            "--bucket",
            bucket,
            "--key",
            key,
            "--expected-bucket-owner",
            owner,
            str(destination),
        )
        if metadata.get("VersionId") != expected_version:
            raise ValueError("Remote state version changed after review; repair refused")
        remote = read_state(destination)
        verify_migration_metadata_only(local, remote, expected_lineage)
        # Preserve every resource/output byte semantically, and retain the S3 version.
        # Conditional write prevents overwriting an intervening non-Terraform writer.
        repaired = folder / "metadata-repair-source.json"
        write_private(repaired, local)
        result = aws.call(
            "s3api",
            "put-object",
            "--bucket",
            bucket,
            "--key",
            key,
            "--body",
            str(repaired),
            "--if-match",
            metadata["ETag"],
            "--expected-bucket-owner",
            owner,
            "--server-side-encryption",
            "AES256",
            "--content-type",
            "application/json",
        )
        write_private(
            folder / "metadata-repair-receipt.json",
            {
                "repaired_at": utc_now(),
                "actor": identity["Arn"],
                "before_version": expected_version,
                "after_version": result.get("VersionId"),
                "source_sha256": state_fingerprint(local),
                "lineage": expected_lineage,
            },
        )
    finally:
        # Only this process's lock can be removed. A conflicting lock is retained.
        observed_lock = folder / "observed-repair-lock.json"
        write_private(observed_lock, "")
        aws.call(
            "s3api",
            "get-object",
            "--bucket",
            bucket,
            "--key",
            key + ".tflock",
            "--expected-bucket-owner",
            owner,
            str(observed_lock),
        )
        if json.loads(observed_lock.read_text()).get("ID") != lock_id:
            raise ValueError("Lock ownership changed; refusing to remove it")
        aws.call(
            "s3api",
            "delete-object",
            "--bucket",
            bucket,
            "--key",
            key + ".tflock",
            "--expected-bucket-owner",
            owner,
        )
    mode, _, remote = bootstrap_status(aws)
    if mode != "remote" or remote != local:
        raise ValueError("Remote repair verification failed; all recovery copies are retained")
    print("Original bootstrap lineage/serial restored; identical payload and S3 versions verified")


def classify_bootstrap(bucket_exists: bool, local: dict | None, remote: dict | None) -> str:
    if remote is not None:
        if not bucket_exists:
            raise ValueError("Remote state without its bucket is inconsistent")
        if local is not None:
            if local["lineage"] != remote["lineage"]:
                raise ValueError("Conflicting bootstrap state lineages; manual recovery required")
            if local["serial"] > remote["serial"]:
                raise ValueError(
                    "Local bootstrap state is newer than S3; refusing to overwrite either"
                )
            if local["serial"] == remote["serial"] and state_fingerprint(
                local
            ) != state_fingerprint(remote):
                raise ValueError(
                    "Equal state serials contain different data; manual recovery required"
                )
        return "remote"
    if local is not None:
        return "recover-local"
    if bucket_exists:
        raise ValueError(
            "State bucket exists without remote or local bootstrap state; "
            "do not create duplicate ownership"
        )
    return "new-local"


def bootstrap_status(aws: Aws) -> tuple[str, dict | None, dict | None]:
    owner = CONFIG["account_id"]
    bucket = aws.call(
        "s3api",
        "head-bucket",
        "--bucket",
        CONFIG["state_bucket"],
        "--expected-bucket-owner",
        owner,
        missing=True,
    )
    local = read_state(state_home() / "bootstrap/terraform.tfstate")
    remote = None
    if bucket is not None:
        destination = private_dir(state_home() / "bootstrap") / "remote-observation.json"
        # Pre-create privately before the CLI writes the downloaded state body.
        write_private(destination, "")
        result = aws.call(
            "s3api",
            "get-object",
            "--bucket",
            CONFIG["state_bucket"],
            "--key",
            STATE_KEYS["bootstrap"],
            "--expected-bucket-owner",
            owner,
            str(destination),
            missing=True,
        )
        if result is not None:
            remote = read_state(destination)
        else:
            destination.unlink(missing_ok=True)
    for state in (local, remote):
        if state:
            bucket_output = state.get("outputs", {}).get("state_bucket", {}).get("value")
            if bucket_output is not None and bucket_output != CONFIG["state_bucket"]:
                raise ValueError("Bootstrap state belongs to a different bucket")
            for resource in state.get("resources", []):
                if resource.get("type") == "aws_iam_role":
                    for instance in resource.get("instances", []):
                        arn = instance.get("attributes", {}).get("arn", "")
                        if arn and not arn.startswith(f"arn:aws:iam::{owner}:role/"):
                            raise ValueError(
                                "Bootstrap state contains a role from a different account"
                            )
    return classify_bootstrap(bucket is not None, local, remote), local, remote


def override_path(root: str) -> Path:
    return ROOTS[root] / "local_backend_override.tf"


def backend_config(root: str) -> Path:
    path = state_home() / "backends" / f"{root}.hcl"
    write_private(
        path,
        "\n".join(
            [
                f'bucket = "{CONFIG["state_bucket"]}"',
                f'key = "{STATE_KEYS[root]}"',
                f'region = "{CONFIG["region"]}"',
                "encrypt = true",
                "use_lockfile = true",
                f'allowed_account_ids = ["{CONFIG["account_id"]}"]',
                "",
            ]
        ),
    )
    return path


def initialize(root: str, *, preview: bool = False) -> str:
    profile = PROFILES["bootstrap"] if preview else PROFILES[root]
    aws = Aws(profile)
    aws.preflight("bootstrap" if preview else root)
    mode = "remote"
    if root == "bootstrap":
        mode, _, _ = bootstrap_status(aws)
    elif not preview:
        if (
            aws.call(
                "s3api",
                "head-bucket",
                "--bucket",
                CONFIG["state_bucket"],
                "--expected-bucket-owner",
                CONFIG["account_id"],
                missing=True,
            )
            is None
        ):
            raise ValueError("Bootstrap and migrate the state bucket first; --preview is read-only")
    if preview or mode != "remote":
        path = state_home() / (
            f"preview/{root}.tfstate" if preview else "bootstrap/terraform.tfstate"
        )
        if preview and path.exists():
            prior = read_state(path)
            if prior and prior.get("resources"):
                raise ValueError(
                    "A preview backend contains applied resources; investigate before continuing"
                )
        private_dir(path.parent)
        write_private(
            override_path(root),
            f'terraform {{\n  backend "local" {{\n    path = {json.dumps(str(path))}\n  }}\n}}\n',
        )
        print(tf(root, "init", "-input=false", "-reconfigure", profile=profile))
        return "preview" if preview else mode
    override_path(root).unlink(missing_ok=True)
    print(
        tf(
            root,
            "init",
            "-input=false",
            "-reconfigure",
            f"-backend-config={backend_config(root)}",
            profile=profile,
        )
    )
    return "remote"


def migrate_bootstrap() -> None:
    aws = Aws(PROFILES["bootstrap"])
    aws.preflight("bootstrap")
    mode, local, remote = bootstrap_status(aws)
    if local is not None:
        write_private(state_home() / "bootstrap/recovery-before-migration.json", local)
    if mode == "new-local":
        raise ValueError("Bootstrap has not been applied; there is no state to migrate")
    if mode == "recover-local":
        override_path("bootstrap").unlink(missing_ok=True)
        print(
            tf(
                "bootstrap",
                "init",
                "-input=false",
                "-migrate-state",
                "-force-copy",
                f"-backend-config={backend_config('bootstrap')}",
            )
        )
    else:
        initialize("bootstrap")
    after_mode, _, after = bootstrap_status(aws)
    if after_mode != "remote" or after is None:
        raise ValueError(
            "Remote bootstrap state could not be verified; local recovery was retained"
        )
    if local and (after["lineage"] != local["lineage"] or after["serial"] < local["serial"]):
        raise ValueError("Migration did not preserve bootstrap state ownership")
    print("Remote bootstrap state verified; private recovery copy retained")
