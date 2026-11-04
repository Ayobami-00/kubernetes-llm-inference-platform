"""Check exact native GPU reports against a bounded, versioned applicability review."""

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from common import ROOT, utc_now
from gpu_operator import boundary, effective_values, settings

REVIEW = ROOT / "deploy/gpu-runtime-risk-review.json"


def sha(body):
    return hashlib.sha256(body).hexdigest()


def digest(value):
    return sha(json.dumps(value, sort_keys=True).encode())


def configuration():
    return {
        name: sha((ROOT / "deploy" / name).read_bytes())
        for name in ("gpu-operator.lock.json", "gpu-host.lock.json", "artifacts.lock.json")
    } | {
        "values": sha(yaml.safe_dump(effective_values()).encode()),
        "boundary": sha(boundary().encode()),
    }


def recent(value, now):
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None or not timedelta(0) <= now - timestamp <= timedelta(hours=24):
        raise ValueError("GPU runtime evidence timestamp is stale or invalid")


def native(path, expected, now):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Unsafe GPU runtime report")
    body = path.read_bytes()
    report = json.loads(body)
    if sha(body) != expected or report.get("Trivy", {}).get("Version") != "0.76.1":
        raise ValueError("GPU runtime native scan identity differs")
    recent(report["CreatedAt"], now)
    if not any(r.get("Packages") for r in report.get("Results", [])):
        raise ValueError("GPU runtime native scan lacks package coverage")
    return report


def findings(report):
    return sorted(
        {
            (v["VulnerabilityID"], v["PkgName"], v["InstalledVersion"], v["Severity"])
            for r in report.get("Results", [])
            for v in r.get("Vulnerabilities", []) or []
            if v["Severity"] in {"CRITICAL", "HIGH"}
        }
    )


def groups(report):
    rows = findings(report)
    return {
        "kernel_headers": [r for r in rows if r[1] == "linux-libc-dev"],
        "runtime": [r for r in rows if r[1] != "linux-libc-dev"],
    }


def check_findings(report, reviewed):
    actual = groups(report)
    if set(reviewed) != set(actual):
        raise ValueError("GPU finding review groups differ")
    for name, rows in actual.items():
        expected = reviewed[name]
        if expected["count"] != len(rows) or expected["sha256"] != digest(rows):
            raise ValueError("Unreviewed GPU runtime finding")
        if rows and not expected.get("rationale"):
            raise ValueError("Missing GPU runtime applicability rationale")
        if name == "runtime" and any(row[3] == "CRITICAL" for row in rows):
            raise ValueError("Critical runtime implementation finding prohibits admission")
    if actual["kernel_headers"]:
        packages = [
            p
            for r in report["Results"]
            for p in r.get("Packages", [])
            if p.get("Name") == "linux-libc-dev"
        ]
        if len(packages) != 1:
            raise ValueError("Ambiguous kernel-header package inventory")
        files = packages[0].get("InstalledFiles", [])
        expected = reviewed["kernel_headers"]
        if (
            not files
            or len(files) != expected["file_count"]
            or digest(sorted(files)) != expected["files_sha256"]
            or any(
                not (
                    (p.startswith("/usr/include/") and p.endswith(".h"))
                    or p.startswith("/usr/share/doc/linux-libc-dev/")
                )
                for p in files
            )
        ):
            raise ValueError("Kernel-header component contains unreviewed runtime files")
    return {name: len(rows) for name, rows in actual.items()}


def assess(image_directory, host_directory, *, now=None):
    now = now or datetime.now(UTC)
    review = json.loads(REVIEW.read_text())
    start, expiry = (datetime.fromisoformat(review[k]) for k in ("reviewed_at", "expires_at"))
    if (
        review.get("schema_version") != "klip-gpu-runtime-risk-review.v1"
        or review.get("profile") != "single-a10"
        or not start <= now < expiry
        or expiry - start > timedelta(days=7)
        or review["configuration"] != configuration()
    ):
        raise ValueError("GPU runtime review profile, configuration or expiry differs")
    images = {r + "@" + v["digest"] for r, v in settings()["images"].items()}
    if set(review["images"]) != images:
        raise ValueError("GPU runtime review image set differs")
    result = {}
    for image, item in review["images"].items():
        name = item["directory"]
        if Path(name).name != name:
            raise ValueError("Invalid GPU scan directory identity")
        folder = image_directory / name
        report = native(folder / "scan.json", item["report_sha256"], now)
        summary = json.loads((folder / "summary.json").read_text())
        recent(summary["database"]["UpdatedAt"], now)
        if (
            report.get("ArtifactName") != image
            or report.get("Metadata", {}).get("ImageConfig", {}).get("architecture") != "amd64"
            or report.get("Metadata", {}).get("ImageConfig", {}).get("os") != "linux"
            or summary["report_sha256"] != item["report_sha256"]
        ):
            raise ValueError("GPU runtime image/report identity differs")
        result[image] = {
            "report_sha256": item["report_sha256"],
            "reviewed": check_findings(report, item["groups"]),
        }
    host = review["host_binaries"]
    report = native(host_directory / "scan.json", host["report_sha256"], now)
    summary = json.loads((host_directory / "summary.json").read_text())
    recent(summary["database"]["UpdatedAt"], now)
    if summary["files"] != host["files"] or summary["report_sha256"] != host["report_sha256"]:
        raise ValueError("GPU host executable identities differ")
    targets = {
        Path(r.get("Target", "")).name
        for r in report["Results"]
        if r.get("Type") == "gobinary" and r.get("Packages")
    }
    if not {"containerd", "containerd-shim-runc-v2", "ctr", "runc"} <= targets:
        raise ValueError("GPU host executable scan coverage differs")
    checked_host = check_findings(report, host["groups"])
    return {
        "schema_version": "klip-gpu-runtime-assessment.v1",
        "status": "conditionally-reviewed-single-a10-bootstrap-only",
        "assessed_at": utc_now(),
        "review_sha256": sha(REVIEW.read_bytes()),
        "profile": review["profile"],
        "configuration": configuration(),
        "expires_at": review["expires_at"],
        "images": result,
        "host_binaries": {"report_sha256": host["report_sha256"], "reviewed": checked_host},
        "limitations": review["limitations"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-directory", type=Path, required=True)
    parser.add_argument("--host-directory", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(assess(args.image_directory, args.host_directory), indent=2))
