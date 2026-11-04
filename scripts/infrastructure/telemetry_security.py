"""Time-bounded exact-image review for dashboards, log/trace storage and ingestion."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from common import ROOT
from observability_release import BACKENDS, backend_values, image
from observability_security import digest
from platform_services import render

REVIEW = ROOT / "deploy/telemetry-risk-review.json"


def configuration(component):
    return {
        "values_sha256": digest(yaml.safe_dump(backend_values(component), sort_keys=True).encode()),
        "boundary_sha256": digest(render("klip-observability", {}).encode()),
    }


def assess_component(directory: Path, component: str, *, now=None):
    now = now or datetime.now(UTC)
    review = json.loads(REVIEW.read_text())
    start, expiry = (datetime.fromisoformat(review[k]) for k in ("reviewed_at", "expires_at"))
    if not start <= now < expiry or expiry - start > timedelta(days=7):
        raise ValueError("Telemetry runtime review expired or exceeds seven days")
    if review["configuration"][component] != configuration(component):
        raise ValueError("Telemetry runtime boundary differs from its review")
    summary = json.loads((directory / "summary.json").read_text())
    completed = datetime.fromisoformat(summary["completed_at"])
    if (
        summary["source_commit"] != review["scan_source_commit"]
        or not timedelta(0) <= now - completed <= timedelta(hours=24)
        or not summary["scanner_version"].startswith("Version: 0.76.1")
    ):
        raise ValueError("Telemetry scan source, freshness or version differs")
    pinned = image(BACKENDS[component])
    expected = review["images"][pinned]
    name = expected["report_file"]
    if Path(name).name != name:
        raise ValueError("Unexpected report filename")
    path = directory / name
    if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Invalid telemetry scan file")
    data = path.read_bytes()
    scan = json.loads(data)
    if (
        digest(data) != expected["report_sha256"]
        or scan.get("ArtifactName") != pinned
        or not any(r.get("Type") == "gobinary" for r in scan.get("Results", []))
    ):
        raise ValueError("Telemetry scan checksum, image identity or coverage differs")
    findings = [f for r in scan.get("Results", []) for f in r.get("Vulnerabilities", []) or []]
    if any(f["Severity"] == "CRITICAL" for f in findings):
        raise ValueError("Critical telemetry vulnerabilities prohibit rollout")
    actual = {
        (f["VulnerabilityID"], f["PkgName"], f["InstalledVersion"])
        for f in findings
        if f["Severity"] == "HIGH"
    }
    allowed = {(f["id"], f["package"], f["version"]): f for f in expected["high_findings"]}
    if actual != set(allowed) or any(
        f["condition"] not in review["conditions"] for f in allowed.values()
    ):
        raise ValueError("Unreviewed High telemetry findings")
    return {
        "status": "conditionally-reviewed",
        "review_sha256": digest(REVIEW.read_bytes()),
        "image": pinned,
        "high_unique_findings": len(actual),
        "raw_high_findings": sum(f["Severity"] == "HIGH" for f in findings),
        "expires_at": review["expires_at"],
        "limitations": (
            "Vulnerabilities remain; restrictions mitigate exposure "
            "and do not establish absence of risk."
        ),
    }
