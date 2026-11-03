"""Stdlib-only execution wrapper copied into each immutable attempt directory.

The persistent mkdir claim is intentionally never removed. A replacement Pod observes
the original attempt instead of restarting its benchmark clock.
"""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def atomic(path, data):
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".bridge-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def execute(manifest_path):
    manifest_path = Path(manifest_path).resolve()
    directory = manifest_path.parent
    manifest = json.loads(manifest_path.read_text())
    cancelled = directory / "cancel-requested"
    complete = directory / "bridge-complete.json"
    claim = directory / "started"
    if cancelled.exists():
        return 130
    if (
        hashlib.sha256(Path(manifest["snapshot_path"]).read_bytes()).hexdigest()
        != manifest["snapshot_sha256"]
    ):
        return 65
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        claim.mkdir()  # Atomic across Pods on the shared filesystem.
    except FileExistsError:
        # Do not return success early: that can make Kubernetes stop the winning Pod.
        while not complete.exists():
            if stopping or cancelled.exists():
                return 130
            time.sleep(0.2)
        return int(json.loads(complete.read_text())["returncode"])
    atomic(
        claim / "owner.json",
        {
            "pod_uid": os.environ.get("KLIP_POD_UID", "local"),
            "attempt_id": manifest["attempt_id"],
            "snapshot_sha256": manifest["snapshot_sha256"],
        },
    )
    if cancelled.exists():
        atomic(complete, {"returncode": 130})
        return 130
    atomic(directory / "bridge-state.json", {"status": "running", "stage": "preparing"})
    environment = {**os.environ, "KLIP_EXECUTION_MANIFEST": str(manifest_path)}
    try:
        with (
            (directory / "stdout.log").open("ab") as out,
            (directory / "stderr.log").open("ab") as err,
        ):
            child = subprocess.Popen(
                manifest["argv"],
                cwd=directory,
                env=environment,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            signalled_at = None
            while child.poll() is None:
                if (stopping or cancelled.exists()) and signalled_at is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    signalled_at = time.monotonic()
                if signalled_at and time.monotonic() - signalled_at > 30:
                    os.killpg(child.pid, signal.SIGKILL)
                time.sleep(0.1)
            code = 130 if signalled_at else child.returncode
    except OSError:
        code = 70
    atomic(complete, {"returncode": code})
    return code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    sys.exit(execute(args.manifest))


if __name__ == "__main__":
    main()
