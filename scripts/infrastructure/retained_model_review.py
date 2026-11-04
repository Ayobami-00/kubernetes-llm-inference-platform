"""Exact applicability checks for the single retained A10 experiment, not a clean scan."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from common import ROOT


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assess(directory, *, now=None):
    now = now or datetime.now(UTC)
    review = json.loads((ROOT / "deploy/retained-a10-model-risk-review.json").read_text())
    summary = json.loads((directory / "summary.json").read_text())
    report = json.loads((directory / "scan.json").read_text())
    start, end = (datetime.fromisoformat(review[k]) for k in ("reviewed_at", "expires_at"))
    if (
        review.get("schema_version") != "klip-retained-a10-model-review.v1"
        or set(review.get("conditions", {}))
        != {"kernel_headers", "fixed_inputs", "no_asn1_input", "brotli", "isolation"}
        or not all(review["conditions"].values())
        or review["scope"] != "private-authenticated-writerassist-experiment"
        or review["instance_id"] != "8f2e29b7279b42148ffaaed4a4dcdd8e"
        or not start <= now < end
        or end - start > timedelta(hours=24)
        or summary["image"] != review["image"]
        or report["ArtifactName"] != review["image"]
        or report["Metadata"]["ImageConfig"]["architecture"] != "amd64"
        or summary["scanner_version"] != "Version: 0.76.1"
        or not timedelta(0)
        <= now - datetime.fromisoformat(summary["observed_at"])
        <= timedelta(hours=24)
        or not timedelta(0)
        <= now - datetime.fromisoformat(summary["database"]["UpdatedAt"])
        <= timedelta(hours=24)
    ):
        raise ValueError("Retained experiment review identity or freshness differs")
    for filename, key in (
        ("scan.json", "report_sha256"),
        ("header-inventory.json", "header_inventory_sha256"),
        ("patch-receipt.json", "patch_receipt_sha256"),
    ):
        if digest(directory / filename) != review[key] or summary[key] != review[key]:
            raise ValueError("Native model evidence differs from its exact review")
    headers = json.loads((directory / "header-inventory.json").read_text())
    if headers["package"] != "linux-libc-dev" or headers["version"] != "6.8.0-147.147":
        raise ValueError("Header inventory belongs to a different package")
    files = [v for v in headers["files"] if v["kind"] != "directory"]
    if len(files) != 990 or any(
        f["kind"] != "file"
        or f["mode"] & 0o111
        or not (
            PurePosixPath(f["path"]).is_relative_to("/usr/include")
            and f["path"].endswith(".h")
            or f["path"]
            in {
                "/usr/share/doc/linux-libc-dev/changelog.Debian.gz",
                "/usr/share/doc/linux-libc-dev/copyright",
            }
        )
        for f in files
    ):
        raise ValueError("Kernel-header inventory contains unexpected executable content")
    findings = [v for t in report["Results"] for v in t.get("Vulnerabilities", [])]
    critical = [v for v in findings if v["Severity"] == "CRITICAL"]
    if len(critical) != 15 or any(
        v["PkgName"] != "linux-libc-dev" or v["InstalledVersion"] != headers["version"]
        for v in critical
    ):
        raise ValueError("A runtime Critical finding prohibits the experiment")
    runtime_high = {
        (v["VulnerabilityID"], v["PkgName"], v["InstalledVersion"])
        for v in findings
        if v["Severity"] == "HIGH" and v["PkgName"] != "linux-libc-dev"
    }
    if runtime_high != {tuple(v["finding"]) for v in review["runtime_high_findings"]}:
        raise ValueError("Unreviewed model runtime High findings")
    return {
        "scope": review["scope"],
        "image": review["image"],
        "report_sha256": review["report_sha256"],
        "critical_header_findings": 15,
        "runtime_critical_findings": 0,
        "runtime_high_findings": len(runtime_high),
        "expires_at": review["expires_at"],
        "limitations": review["limitations"],
    }
