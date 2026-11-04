"""Scan one immutable public GPU image on an isolated CPU worker; never approve deployment."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "deploy/gpu-operator.lock.json"


def scan(reference, directory, *, lock_path=None, kind="gpu"):
    if kind not in {"gpu", "inference"}:
        raise ValueError("Unknown public image scan kind")
    lock_path = lock_path or LOCK
    lock = json.loads(lock_path.read_text())
    item = lock["images"].get(reference)
    if (
        item is None
        or item["platform"] != "linux/amd64"
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", item["digest"])
    ):
        raise ValueError("Image must be selected from the checked GPU lock")
    directory.mkdir(parents=True, exist_ok=False)
    config = directory / "anonymous-registry"
    config.mkdir(mode=0o700)
    (config / "config.json").write_text("{}\n")
    cache = Path(os.environ.get("RUNNER_TEMP", directory.parent)) / "gpu-scan-cache"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("AWS_", "DOCKER_", "TRIVY_")) and k not in {"GITHUB_TOKEN", "GH_TOKEN"}
    }
    env.update(
        DOCKER_CONFIG=str(config.resolve()), TRIVY_CACHE_DIR=str(cache.resolve()), GOMAXPROCS="2"
    )
    version = subprocess.run(
        ["trivy", "--version"], check=True, capture_output=True, text=True
    ).stdout
    if not version.startswith("Version: 0.76.1"):
        raise ValueError("GPU image scan requires the pinned scanner")
    pinned = reference + "@" + item["digest"]
    record = {
        "schema_version": "klip-public-" + kind + "-scan.v1",
        "source_commit": os.environ.get("GITHUB_SHA"),
        "lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        "reference": reference,
        "image": pinned,
        "platform": item["platform"],
        "scanner_version": version.strip(),
        "started_at": datetime.now(UTC).isoformat(),
        "status": "scanning",
        "limitations": [
            "Package scanning does not establish coverage of every proprietary NVIDIA binary.",
            "Findings need an exact runtime review; this command does not authorize GPU admission.",
        ],
    }
    path = directory / "summary.json"
    try:
        subprocess.run(
            ["trivy", "image", "--download-db-only", "--timeout", "5m"],
            env=env,
            check=True,
            timeout=330,
        )
        record["database"] = json.loads((cache / "db/metadata.json").read_text())
        report = directory / "scan.json"
        subprocess.run(
            [
                "trivy",
                "image",
                "--image-src",
                "remote",
                "--platform",
                "linux/amd64",
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
                str(report),
                "--timeout",
                "7m",
                pinned,
            ],
            env=env,
            check=True,
            timeout=450,
        )
        body = report.read_bytes()
        data = json.loads(body)
        if (
            data.get("ArtifactName") != pinned
            or data.get("Metadata", {}).get("ImageConfig", {}).get("architecture") != "amd64"
        ):
            raise ValueError("Native scan image identity differs")
        counts = Counter(
            v["Severity"]
            for r in data.get("Results", [])
            for v in r.get("Vulnerabilities", []) or []
        )
        record.update(
            status="scanned",
            completed_at=datetime.now(UTC).isoformat(),
            report_sha256=hashlib.sha256(body).hexdigest(),
            severity_counts=dict(counts),
            coverage=[
                {
                    "type": r.get("Type"),
                    "target": r.get("Target"),
                    "package_count": len(r.get("Packages", [])),
                }
                for r in data.get("Results", [])
            ],
        )
        if not any(c["package_count"] for c in record["coverage"]):
            raise ValueError("Image scan has no recognized package coverage")
        if counts["CRITICAL"]:
            raise ValueError("Critical findings require remediation before GPU admission")
    except Exception as error:
        record.update(status="review-or-reconciliation-required", error_type=type(error).__name__)
        raise
    finally:
        path.write_text(json.dumps(record, indent=2) + "\n")
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--source", choices=["gpu", "inference"], default="gpu")
    args = parser.parse_args()
    print(
        json.dumps(
            scan(
                args.reference,
                args.directory,
                lock_path=LOCK if args.source == "gpu" else ROOT / "deploy/inference.lock.json",
                kind=args.source,
            ),
            indent=2,
        )
    )
