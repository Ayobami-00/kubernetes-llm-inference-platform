"""Scan the exact Linux GPU host binaries without executing them or changing hosts."""

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from common import ROOT, tool, utc_now
from gpu_runtime import install_artifact, settings
from host_executor_image import download


def scan(directory, *, database_cache=None):
    directory.mkdir(parents=True, exist_ok=False)
    source = directory / "binaries"
    source.mkdir()
    archive = install_artifact()
    config = settings()
    files = {}
    # install_artifact verifies size, checksum and every archive entry first.
    with tarfile.open(archive, "r:gz") as bundle:
        for entry in bundle.getmembers():
            if not entry.isfile():
                continue
            path = source / entry.name
            path.parent.mkdir(parents=True, exist_ok=True)
            body = bundle.extractfile(entry).read()
            path.write_bytes(body)
            path.chmod(0o555)
            files[entry.name] = hashlib.sha256(body).hexdigest()
    lock = json.loads((ROOT / "deploy/artifacts.lock.json").read_text())
    item = lock["artifacts"]["runc-linux-amd64"]
    cached_runc = ROOT / "artifacts/platform/runc-linux-amd64"
    body = cached_runc.read_bytes() if cached_runc.is_file() else download(item)
    if (
        len(body) > item.get("max_bytes", 128 * 1024 * 1024)
        or hashlib.sha256(body).hexdigest() != item["sha256"]
    ):
        raise ValueError("GPU host runc artifact checksum or size differs")
    (source / "runc").write_bytes(body)
    (source / "runc").chmod(0o555)
    files["runc"] = hashlib.sha256(body).hexdigest()
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("AWS_", "TRIVY_", "DOCKER_")) and k not in {"GH_TOKEN", "GITHUB_TOKEN"}
    }
    anonymous = directory / "anonymous-registry"
    anonymous.mkdir(mode=0o700)
    (anonymous / "config.json").write_text("{}\n")
    cache = (database_cache or directory / "cache").resolve()
    env.update(
        TRIVY_CACHE_DIR=str(cache),
        GOMAXPROCS="2",
        DOCKER_CONFIG=str(anonymous.resolve()),
    )
    version = subprocess.check_output([tool("trivy"), "--version"], text=True)
    if not version.startswith("Version: 0.76.1"):
        raise ValueError("Expected the pinned host-binary scanner")
    if database_cache is None:
        subprocess.run(
            [tool("trivy"), "image", "--download-db-only", "--timeout", "5m"],
            env=env,
            check=True,
            timeout=330,
            capture_output=True,
        )
    database = json.loads((cache / "db/metadata.json").read_text())
    if (
        not timedelta(0)
        <= datetime.now(UTC) - datetime.fromisoformat(database["UpdatedAt"].replace("Z", "+00:00"))
        <= timedelta(hours=24)
    ):
        raise ValueError("GPU host scan database must be less than 24 hours old")
    path = directory / "scan.json"
    subprocess.run(
        [
            tool("trivy"),
            "rootfs",
            "--scanners",
            "vuln",
            "--skip-db-update",
            "--skip-java-db-update",
            "--ignorefile",
            "/dev/null",
            "--config",
            "/dev/null",
            "--list-all-pkgs",
            "--format",
            "json",
            "--output",
            str(path),
            "--timeout",
            "5m",
            str(source),
        ],
        env=env,
        check=True,
        timeout=330,
        capture_output=True,
    )
    report = json.loads(path.read_text())
    targets = {
        Path(r.get("Target", "")).name
        for r in report.get("Results", [])
        if r.get("Type") == "gobinary"
    }
    if not {"containerd", "containerd-shim-runc-v2", "ctr", "runc"} <= targets:
        raise ValueError("GPU host scan lacks required executable coverage")
    findings = [v for r in report["Results"] for v in r.get("Vulnerabilities", []) or []]
    summary = {
        "schema_version": "klip-gpu-host-security.v1",
        "completed_at": utc_now(),
        "scanner_version": version.strip(),
        "database": database,
        "host_lock_sha256": hashlib.sha256(
            (ROOT / "deploy/gpu-host.lock.json").read_bytes()
        ).hexdigest(),
        "base_lock_sha256": hashlib.sha256(
            (ROOT / "deploy/artifacts.lock.json").read_bytes()
        ).hexdigest(),
        "containerd_archive_sha256": config["containerd_artifact"]["sha256"],
        "runc_sha256": item["sha256"],
        "files": files,
        "report_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "critical_findings": sorted(
            {
                (v["VulnerabilityID"], v["PkgName"], v["InstalledVersion"])
                for v in findings
                if v["Severity"] == "CRITICAL"
            }
        ),
        "high_findings": sorted(
            {
                (v["VulnerabilityID"], v["PkgName"], v["InstalledVersion"])
                for v in findings
                if v["Severity"] == "HIGH"
            }
        ),
        "limitations": [
            "Static binary scan only; no host execution, driver, kernel or GPU "
            "readiness is established."
        ],
    }
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--database-cache", type=Path)
    args = parser.parse_args()
    print(json.dumps(scan(args.directory, database_cache=args.database_cache), indent=2))
