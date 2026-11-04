"""Assess exact monitoring scans against a time-bounded runtime review."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from common import ROOT
from observability_release import LOCK, image, monitoring_values, thanos_values
from platform_services import render

REVIEW = ROOT / "deploy/observability-risk-review.json"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def configuration_hashes() -> dict:
    return {
        "values_sha256": digest(yaml.safe_dump(monitoring_values(), sort_keys=True).encode()),
        "thanos_values_sha256": digest(yaml.safe_dump(thanos_values(), sort_keys=True).encode()),
        "boundary_sha256": digest(render("klip-observability", {}).encode()),
        "chart_sha256": json.loads(LOCK.read_text())["charts"]["kube-prometheus-stack"]["sha256"],
    }


def assess(directory: Path, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    review = json.loads(REVIEW.read_text())
    start = datetime.fromisoformat(review["reviewed_at"])
    expiry = datetime.fromisoformat(review["expires_at"])
    if not start <= now < expiry or expiry - start > timedelta(days=7):
        raise ValueError("Monitoring runtime review expired or exceeds seven days")
    if review["configuration"] != configuration_hashes():
        raise ValueError("Monitoring configuration differs from its reviewed runtime boundary")
    summary = json.loads((directory / "summary.json").read_text())
    completed = datetime.fromisoformat(summary["completed_at"])
    if (
        summary["source_commit"] != review["scan_source_commit"]
        or not timedelta(0) <= now - completed <= timedelta(hours=24)
        or not summary["scanner_version"].startswith("Version: 0.76.1")
    ):
        raise ValueError("Monitoring scan source, scanner or freshness differs")
    images = {}
    for reference in json.loads(LOCK.read_text())["images"]:
        pinned = image(reference)
        expected = review["images"].get(pinned)
        if not expected:
            raise ValueError("Unreviewed monitoring image: " + reference)
        filename = expected["report_file"]
        if Path(filename).name != filename:
            raise ValueError("Invalid scan filename")
        path = directory / filename
        if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Invalid monitoring scan file")
        data = path.read_bytes()
        if digest(data) != expected["report_sha256"]:
            raise ValueError("Monitoring scan checksum differs")
        scan = json.loads(data)
        if scan.get("ArtifactName") != pinned or not any(
            entry.get("Type") == "gobinary" for entry in scan.get("Results", [])
        ):
            raise ValueError("Monitoring scan identity or Go coverage differs")
        findings = [
            finding
            for entry in scan["Results"]
            for finding in entry.get("Vulnerabilities", []) or []
        ]
        if any(f["Severity"] == "CRITICAL" for f in findings):
            raise ValueError("Critical monitoring findings prohibit deployment")
        high = {
            (f["VulnerabilityID"], f["PkgName"], f["InstalledVersion"])
            for f in findings
            if f["Severity"] == "HIGH"
        }
        allowed = {(f["id"], f["package"], f["version"]): f for f in expected["high_findings"]}
        if high != set(allowed) or any(
            f.get("condition") not in review["conditions"] for f in allowed.values()
        ):
            raise ValueError("Unreviewed or changed High monitoring findings")
        images[pinned] = {"high_findings": len(high), "report_sha256": digest(data)}
    if set(review["images"]) != set(images):
        raise ValueError("Review contains stale or extra image identities")
    return {
        "status": "conditionally-reviewed",
        "review_sha256": digest(REVIEW.read_bytes()),
        "configuration": review["configuration"],
        "expires_at": review["expires_at"],
        "images": images,
        "limitations": [
            "High findings remain; private runtime restrictions mitigate their reviewed exposure",
            "Static analysis is conservative and cannot establish absence of vulnerabilities",
            "Review expiry blocks new rollout; it does not silently disable a running monitor",
        ],
    }
