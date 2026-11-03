"""Recoverable local-to-S3 bootstrap. No state is discarded or blindly overwritten."""

from __future__ import annotations

import hashlib
import json
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
