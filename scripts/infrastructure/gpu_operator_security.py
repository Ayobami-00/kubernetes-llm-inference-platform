"""Bind the CPU-only GPU controllers to exact scans and a short runtime review.

This does not approve NVIDIA driver/toolkit/device workloads for live GPU use.
Those images and their host runtime need their own deployment evidence.
"""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from common import ROOT, private_dir, tool, utc_now, write_private
from gpu_operator import LOCK, boundary, effective_values, image, sha

REVIEW = ROOT / "deploy/gpu-controller-risk-review.json"
REFERENCES = (
    "nvcr.io/nvidia/gpu-operator:v26.7.2",
    "registry.k8s.io/nfd/node-feature-discovery:v0.20.1",
)
FILENAMES = ("operator.json", "nfd-0.20.1.json")


def scan(directory):
    private_dir(directory)
    if any((directory / name).exists() for name in (*FILENAMES, "scan-context.json")):
        raise ValueError("Retain prior scans; choose a new output directory")
    config = private_dir(directory / "anonymous-registry")
    write_private(config / "config.json", {})
    cache = ROOT / "artifacts/security/gpu-controller-cache"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "DOCKER_", "TRIVY_"))}
    env.update(DOCKER_CONFIG=str(config.resolve()), TRIVY_CACHE_DIR=str(cache), GOMAXPROCS="2")
    version = subprocess.run(
        [tool("trivy"), "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if not version.startswith("Version: 0.76.1"):
        raise ValueError("Unexpected GPU controller scanner version")
    subprocess.run(
        [tool("trivy"), "image", "--download-db-only", "--timeout", "5m"],
        env=env,
        check=True,
        timeout=330,
    )
    for reference, filename in zip(REFERENCES, FILENAMES, strict=True):
        subprocess.run(
            [
                tool("trivy"),
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
                str(directory / filename),
                "--timeout",
                "5m",
                image(reference),
            ],
            env=env,
            check=True,
            timeout=330,
        )
    context = {
        "completed_at": utc_now(),
        "scanner_version": version,
        "database": json.loads((cache / "db/metadata.json").read_text()),
    }
    write_private(directory / "scan-context.json", context)
    return {"directory": str(directory), **context}


def configuration_hashes():
    return {
        "lock_sha256": sha(LOCK.read_bytes()),
        "values_sha256": sha(yaml.safe_dump(effective_values()).encode()),
        "boundary_sha256": sha(boundary().encode()),
    }


def assess(directory: Path, *, now=None):
    now = now or datetime.now(UTC)
    review = json.loads(REVIEW.read_text())
    start, expiry = (datetime.fromisoformat(review[k]) for k in ("reviewed_at", "expires_at"))
    if not start <= now < expiry or expiry - start > timedelta(days=7):
        raise ValueError("GPU controller review is expired or exceeds seven days")
    if review["configuration"] != configuration_hashes():
        raise ValueError("GPU controller runtime configuration differs from its review")
    context = json.loads((directory / "scan-context.json").read_text())
    if (
        not context["scanner_version"].startswith("Version: 0.76.1")
        or not timedelta(0)
        <= now - datetime.fromisoformat(context["completed_at"])
        <= timedelta(hours=24)
        or not timedelta(0)
        <= now - datetime.fromisoformat(context["database"]["UpdatedAt"].replace("Z", "+00:00"))
        <= timedelta(hours=24)
    ):
        raise ValueError("GPU controller scan or database is stale")
    images = {}
    if set(review["images"]) != {image(r) for r in REFERENCES}:
        raise ValueError("GPU controller image set differs from its review")
    for reference in REFERENCES:
        pinned = image(reference)
        expected = review["images"][pinned]
        filename = expected["report_file"]
        if Path(filename).name != filename:
            raise ValueError("Invalid GPU scan filename")
        path = directory / filename
        if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Invalid GPU controller scan file")
        body = path.read_bytes()
        scan = json.loads(body)
        if (
            sha(body) != expected["report_sha256"]
            or scan.get("ArtifactName") != pinned
            or scan.get("Trivy", {}).get("Version") != "0.76.1"
            or scan.get("Metadata", {}).get("ImageConfig", {}).get("architecture") != "amd64"
            or scan.get("Metadata", {}).get("ImageConfig", {}).get("os") != "linux"
            or not any(r.get("Type") == "gobinary" for r in scan.get("Results", []))
        ):
            raise ValueError("GPU controller scan checksum, identity or Go coverage differs")
        if (
            not timedelta(0)
            <= now - datetime.fromisoformat(scan["CreatedAt"].replace("Z", "+00:00"))
            <= timedelta(hours=24)
        ):
            raise ValueError("Native GPU controller scan timestamp is stale")
        findings = [v for r in scan["Results"] for v in r.get("Vulnerabilities", []) or []]
        if any(v["Severity"] == "CRITICAL" for v in findings):
            raise ValueError("Critical GPU controller finding prohibits deployment")
        actual = {
            (v["VulnerabilityID"], v["PkgName"], v["InstalledVersion"])
            for v in findings
            if v["Severity"] == "HIGH"
        }
        if actual != {tuple(f["finding"]) for f in expected["high_findings"]} or any(
            not f.get("rationale") for f in expected["high_findings"]
        ):
            raise ValueError("Unreviewed GPU controller High findings")
        images[pinned] = {"report_sha256": sha(body), "unique_high_findings": len(actual)}
    return {
        "status": "conditionally-reviewed-cpu-controllers-only",
        "review_sha256": sha(REVIEW.read_bytes()),
        "configuration": configuration_hashes(),
        "expires_at": review["expires_at"],
        "scan_completed_at": context["completed_at"],
        "images": images,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["scan", "assess"])
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps((scan if args.command == "scan" else assess)(args.directory), indent=2))
