"""Execute-only consumer of C's immutable management-runner.v1 handoff."""

import hashlib
import json
import os
import re
from pathlib import Path

from platform_contracts import RunSnapshot

from .artifacts import atomic_json
from .control import execute, initialize_attempt, load_run, publish_observation

SECRET_ROOT = Path("/run/secrets/runner")


def execute_manifest(path: Path, credential_files=()):
    path = path.resolve(strict=True)
    directory = path.parent
    manifest = json.loads(path.read_text())
    if manifest.get("schema_version") != "management-runner.v1":
        raise ValueError("unsupported execution manifest version")
    snapshot_path = Path(manifest["snapshot_path"]).resolve(strict=True)
    if snapshot_path != directory / "snapshot.json":
        raise ValueError("managed snapshot must reside in the attempt directory")
    content = snapshot_path.read_bytes()
    if hashlib.sha256(content).hexdigest() != manifest["snapshot_sha256"]:
        raise ValueError("managed snapshot checksum differs")
    snapshot = RunSnapshot.model_validate_json(content)
    if snapshot.target is None or manifest["fixture"] != (snapshot.target.mode == "local-stub"):
        raise ValueError("manifest fixture flag disagrees with execution target")
    if not re.fullmatch(r"op-[a-z0-9-]+", manifest["attempt_id"]):
        raise ValueError("invalid managed attempt identity")
    # C's bridge owns the permanent start claim. A never launches another process adapter.
    if (directory / "run.json").exists():
        previous = load_run(directory)
        if previous.id != manifest["run_id"] or previous.snapshot != snapshot:
            raise ValueError("attempt already contains different immutable inputs")
        if previous.status not in {"queued", "running"}:
            publish_observation(directory, previous)
            return previous
        raise ValueError("managed attempt already started; automatic restart is forbidden")
    try:
        (directory / ".execute-claimed").mkdir()
    except FileExistsError:
        raise ValueError(
            "managed execution was already claimed; automatic restart is forbidden"
        ) from None
    required = set(snapshot.target.credential_env.values())
    original = {}
    try:
        for mapping in credential_files:
            variable, filename = mapping.split("=", 1)
            if variable not in required:
                raise ValueError("credential file does not match a frozen environment reference")
            secret_path = Path(filename)
            if not secret_path.is_absolute() or not secret_path.resolve().is_relative_to(
                SECRET_ROOT
            ):
                raise ValueError("credential files must use the runner Secret mount")
            if variable in original:
                raise ValueError("duplicate credential file mapping")
            value = secret_path.read_text().strip()
            if not value:
                raise ValueError("credential file is empty")
            original[variable] = os.environ.get(variable)
            os.environ[variable] = value
        run = initialize_attempt(snapshot, directory, run_id=manifest["run_id"])
        atomic_json(
            directory / "execution-identity.json",
            {
                "run_id": run.id,
                "attempt_id": manifest["attempt_id"],
                "snapshot_sha256": manifest["snapshot_sha256"],
                "binding_sha256": manifest["binding_sha256"],
            },
        )
        publish_observation(directory, run)
        return execute(directory)
    finally:
        for variable, value in original.items():
            if value is None:
                os.environ.pop(variable, None)
            else:
                os.environ[variable] = value
