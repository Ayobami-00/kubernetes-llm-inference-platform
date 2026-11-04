"""Full-image scan and independent runtime review for SSH-authorized executor code."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from common import PROFILES, ROOT, environment, private_dir, tool, utc_now, write_private

REVIEW = ROOT / "deploy/host-executor-risk-review.json"


def receipt(path):
    value = json.loads(path.read_text())
    if (
        value.get("schema_version") != "klip-private-image.v1"
        or value.get("component") != "host-executor"
        or not re.fullmatch(r"[a-f0-9]{40}", value.get("source_commit", ""))
        or not re.fullmatch(
            r"127811062476\.dkr\.ecr\.eu-west-2\.amazonaws\.com/klip/management-backend@sha256:[a-f0-9]{64}",
            value.get("pinned_image", ""),
        )
        or value.get("digest") != value["pinned_image"].split("@")[1]
    ):
        raise ValueError("Invalid isolated executor image receipt")
    return value


def scan_environment(*, ci=False):
    if ci:
        if os.environ.get("GITHUB_ACTIONS") != "true":
            raise ValueError("CI scan credentials require the actual GitHub Actions environment")
        env = {
            k: v for k, v in os.environ.items() if k not in {"AWS_PROFILE", "AWS_DEFAULT_PROFILE"}
        }
        identity = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--output", "json", "--region", "eu-west-2"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if identity.returncode:
            raise ValueError("Cannot verify the scoped CI scanner identity")
        value = json.loads(identity.stdout)
        if value.get("Account") != "127811062476" or not value.get("Arn", "").startswith(
            "arn:aws:sts::127811062476:assumed-role/klip-images-push/"
        ):
            raise ValueError("Unexpected CI scanner account or role")
        return env
    env = environment(PROFILES["registry"])
    # AWS CLI resolves the operator's interactive session; secrets remain in
    # child-process environment only. No registry password/STS key is retained.
    exported = subprocess.run(
        ["aws", "configure", "export-credentials", "--format", "process"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if exported.returncode:
        raise ValueError("Cannot resolve scoped registry observation credentials")
    credential = json.loads(exported.stdout)
    env.update(
        {
            "AWS_ACCESS_KEY_ID": credential["AccessKeyId"],
            "AWS_SECRET_ACCESS_KEY": credential["SecretAccessKey"],
            "AWS_SESSION_TOKEN": credential["SessionToken"],
        }
    )
    return env


def scan(path, destination, *, ci=False):
    value = receipt(path)
    private_dir(destination)
    env = scan_environment(ci=ci)
    if ci and value["source_commit"] != os.environ.get("GITHUB_SHA"):
        raise ValueError("CI scan receipt differs from the publishing source")
    env["TRIVY_CACHE_DIR"] = str(ROOT / "artifacts/security/host-executor-cache")
    version = subprocess.run(
        [tool("trivy"), "--version"], capture_output=True, text=True, check=True
    ).stdout
    if not version.startswith("Version: 0.76.1"):
        raise ValueError("Unexpected full-image scanner version")
    subprocess.run(
        [tool("trivy"), "image", "--download-db-only", "--timeout", "5m"], env=env, check=True
    )
    report = destination / "scan.json"
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
            str(report),
            "--timeout",
            "5m",
            value["pinned_image"],
        ],
        env=env,
        check=True,
    )
    database = json.loads((Path(env["TRIVY_CACHE_DIR"]) / "db/metadata.json").read_text())
    summary = {
        "schema_version": "klip-host-security.v1",
        "source_commit": value["source_commit"],
        "image": value["pinned_image"],
        "scanned_at": utc_now(),
        "scanner_version": version.strip(),
        "database": database,
        "scan_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
    }
    write_private(destination / "summary.json", summary)
    return summary


def findings(data):
    return sorted(
        {
            (v["VulnerabilityID"], v["PkgName"], v["InstalledVersion"])
            for result in data.get("Results", [])
            for v in result.get("Vulnerabilities", []) or []
            if v["Severity"] == "HIGH"
        }
    )


def assess(path, directory, *, now=None):
    now = now or datetime.now(UTC)
    image = receipt(path)
    review = json.loads(REVIEW.read_text())
    summary = json.loads((directory / "summary.json").read_text())
    data = (directory / "scan.json").read_bytes()
    report = json.loads(data)
    if (
        summary.get("schema_version") != "klip-host-security.v1"
        or summary["image"] != image["pinned_image"]
        or summary["source_commit"] != image["source_commit"]
        or summary["scan_sha256"] != hashlib.sha256(data).hexdigest()
        or not summary["scanner_version"].startswith("Version: 0.76.1")
        or report.get("ArtifactName") != image["pinned_image"]
        or not {"debian", "python-pkg", "gobinary"}.issubset(
            {r.get("Type") for r in report.get("Results", [])}
        )
        or not timedelta(0)
        <= now - datetime.fromisoformat(summary["scanned_at"])
        <= timedelta(hours=24)
        or not timedelta(0)
        <= now - datetime.fromisoformat(summary["database"]["UpdatedAt"].replace("Z", "+00:00"))
        <= timedelta(hours=24)
    ):
        raise ValueError("Executor scan identity, coverage or freshness differs")
    if any(
        v["Severity"] == "CRITICAL"
        for r in report["Results"]
        for v in r.get("Vulnerabilities", []) or []
    ):
        raise ValueError("Critical executor findings prohibit deployment")
    if (
        review.get("schema_version") != "klip-host-risk-review.v1"
        or review.get("image") != image["pinned_image"]
        or review.get("source_commit") != image["source_commit"]
        or not date.fromisoformat(review["reviewed_at"])
        <= now.date()
        < date.fromisoformat(review["expires_at"])
        or findings(report) != sorted(tuple(f["finding"]) for f in review["high_findings"])
        or any(not f.get("rationale") for f in review["high_findings"])
    ):
        raise ValueError("Executor requires an exact, current independent runtime review")
    return {
        "image": image["pinned_image"],
        "source_commit": image["source_commit"],
        "scan_sha256": summary["scan_sha256"],
        "review_sha256": hashlib.sha256(REVIEW.read_bytes()).hexdigest(),
        "high_findings": [list(value) for value in findings(report)],
        "critical_findings": 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["scan", "assess"])
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--ci-credentials", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            scan(args.receipt, args.directory, ci=args.ci_credentials)
            if args.action == "scan"
            else assess(args.receipt, args.directory),
            indent=2,
        )
    )
