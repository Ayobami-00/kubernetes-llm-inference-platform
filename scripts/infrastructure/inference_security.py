"""Assess exact private-controller findings against a bounded runtime review."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from common import ROOT

REVIEW = ROOT / "deploy/inference-controller-risk-review.json"
COMPONENTS = {"gateway", "ai-controller", "ai-extproc"}


def assess(directory, *, now=None):
    from inference_artifacts import settings
    from inference_platform import configuration_hashes

    now = now or datetime.now(UTC)
    review = json.loads(REVIEW.read_text())
    start, expiry = (datetime.fromisoformat(review[k]) for k in ("reviewed_at", "expires_at"))
    if (
        review.get("schema_version") != "klip-inference-controller-review.v1"
        or not start <= now < expiry
        or expiry - start > timedelta(days=7)
        or review["scope"] != "private-controllers-only"
        or review["configuration"] != configuration_hashes()
        or set(review["components"]) != COMPONENTS
        or not review["conditions"]
    ):
        raise ValueError("Inference controller review scope, configuration or validity differs")
    result = {}
    for name, expected in review["components"].items():
        lock = settings()
        item = lock["images"][lock["scan_components"][name]]
        if expected["image"] != item["reference"] + "@" + item["digest"]:
            raise ValueError("Reviewed controller image differs from the locked component")
        subdirectory = directory / expected["directory"]
        if not subdirectory.resolve().is_relative_to(directory.resolve()):
            raise ValueError("Scan path escapes its evidence directory")
        summary = json.loads((subdirectory / "summary.json").read_text())
        report_bytes = (subdirectory / "scan.json").read_bytes()
        report = json.loads(report_bytes)
        if (
            summary.get("schema_version") != "klip-public-inference-scan.v1"
            or summary.get("status") != "scanned"
            or summary["image"] != expected["image"]
            or report.get("ArtifactName") != expected["image"]
            or report.get("Metadata", {}).get("ImageConfig", {}).get("architecture") != "amd64"
            or hashlib.sha256(report_bytes).hexdigest() != expected["report_sha256"]
            or summary["report_sha256"] != expected["report_sha256"]
            or not summary["scanner_version"].startswith("Version: 0.76.1")
            or not timedelta(0)
            <= now - datetime.fromisoformat(summary["completed_at"])
            <= timedelta(hours=24)
            or not timedelta(0)
            <= now - datetime.fromisoformat(summary["database"]["UpdatedAt"].replace("Z", "+00:00"))
            <= timedelta(hours=24)
            or not any(r.get("Type") == "gobinary" and r.get("Packages") for r in report["Results"])
        ):
            raise ValueError("Inference controller scan identity, coverage or freshness differs")
        findings = [v for r in report["Results"] for v in r.get("Vulnerabilities", []) or []]
        if any(v["Severity"] == "CRITICAL" for v in findings):
            raise ValueError("Critical inference controller findings prohibit deployment")
        high = {
            (v["VulnerabilityID"], v["PkgName"], v["InstalledVersion"])
            for v in findings
            if v["Severity"] == "HIGH"
        }
        allowed = {tuple(v["finding"]) for v in expected["high_findings"]}
        if high != allowed or any(
            v.get("condition") not in review["conditions"] or not v.get("rationale")
            for v in expected["high_findings"]
        ):
            raise ValueError("Unreviewed inference controller findings")
        result[name] = {
            "image": expected["image"],
            "report_sha256": expected["report_sha256"],
            "high_findings": len(high),
            "critical_findings": 0,
        }
    return {
        "status": "conditionally-reviewed",
        "scope": review["scope"],
        "review_sha256": hashlib.sha256(REVIEW.read_bytes()).hexdigest(),
        "expires_at": review["expires_at"],
        "components": result,
        "limitations": [
            "High findings remain. No model, data-plane proxy or public traffic is authorized."
        ],
    }
