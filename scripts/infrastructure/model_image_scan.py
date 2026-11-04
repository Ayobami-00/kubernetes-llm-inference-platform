"""Retain a complete native scan of the private derived model image; never waive findings."""

import hashlib
import json
import os
import re
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


def scan():
    receipt = json.loads(Path("artifacts/releases/model-serving.json").read_text())
    image = receipt["pinned_image"]
    if (
        receipt["component"] != "model-serving"
        or receipt["source_commit"] != os.environ["GITHUB_SHA"]
        or not re.fullmatch(
            r"127811062476\.dkr\.ecr\.eu-west-2\.amazonaws\.com/klip/model-serving@sha256:[a-f0-9]{64}",
            image,
        )
    ):
        raise ValueError("Unexpected derived model image identity")
    directory = Path("artifacts/security/model-serving")
    directory.mkdir(parents=True, exist_ok=True)
    patch = directory / "patch.lock.json"
    if patch.read_bytes() != Path("dev/model-serving/patch.lock.json").read_bytes():
        raise ValueError("Image patch lock differs from its claimed source")
    env = {k: v for k, v in os.environ.items() if not k.startswith("TRIVY_")}
    env.update(
        TRIVY_CACHE_DIR=str(Path(os.environ["RUNNER_TEMP"]) / "klip-model-scan"), GOMAXPROCS="2"
    )
    version = subprocess.check_output(["trivy", "--version"], text=True, env=env)
    if not version.startswith("Version: 0.76.1"):
        raise ValueError("Unexpected model scanner version")
    subprocess.run(
        ["trivy", "image", "--download-db-only", "--timeout", "5m"],
        check=True,
        env=env,
        timeout=330,
    )
    report = directory / "scan.json"
    subprocess.run(
        [
            "trivy",
            "image",
            "--image-src",
            "docker",
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
            "12m",
            image,
        ],
        check=True,
        env=env,
        timeout=750,
    )
    body = report.read_bytes()
    value = json.loads(body)
    if (
        value["ArtifactName"] != image
        or value["Metadata"]["ImageConfig"]["architecture"] != "amd64"
        or not any(r.get("Packages") for r in value.get("Results", []))
    ):
        raise ValueError("Native model scan identity or coverage differs")
    counts = Counter(
        v["Severity"] for r in value["Results"] for v in r.get("Vulnerabilities", []) or []
    )
    summary = {
        "schema_version": "klip-model-image-scan.v1",
        "source_commit": receipt["source_commit"],
        "image": image,
        "observed_at": datetime.now(UTC).isoformat(),
        "scanner_version": version.strip(),
        "database": json.loads((Path(env["TRIVY_CACHE_DIR"]) / "db/metadata.json").read_text()),
        "report_sha256": hashlib.sha256(body).hexdigest(),
        "severity_counts": dict(counts),
        "patch_receipt_sha256": hashlib.sha256(
            (directory / "patch-receipt.json").read_bytes()
        ).hexdigest(),
        "header_inventory_sha256": hashlib.sha256(
            (directory / "header-inventory.json").read_bytes()
        ).hexdigest(),
        "limitations": [
            "All findings are retained. This build does not authorize serving deployment."
        ],
    }
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    if counts["CRITICAL"]:
        raise ValueError(
            "Critical findings require exact remediation/applicability review before serving"
        )


if __name__ == "__main__":
    scan()
