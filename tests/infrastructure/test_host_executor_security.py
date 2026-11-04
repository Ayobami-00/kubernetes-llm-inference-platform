"""SSH authority cannot be deployed from incomplete, stale or newly vulnerable scans."""

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta

import host_executor_security as security
import pytest

IMAGE = "127811062476.dkr.ecr.eu-west-2.amazonaws.com/klip/management-backend@sha256:" + "a" * 64


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    now = datetime(2026, 11, 4, 4, tzinfo=UTC)
    image = {
        "schema_version": "klip-private-image.v1",
        "component": "host-executor",
        "source_commit": "a" * 40,
        "pinned_image": IMAGE,
        "digest": "sha256:" + "a" * 64,
    }
    report = {
        "ArtifactName": IMAGE,
        "Results": [
            {"Type": "debian", "Vulnerabilities": []},
            {"Type": "python-pkg", "Vulnerabilities": []},
            {"Type": "gobinary", "Vulnerabilities": []},
        ],
    }
    summary = {
        "schema_version": "klip-host-security.v1",
        "source_commit": "a" * 40,
        "image": IMAGE,
        "scanned_at": now.isoformat(),
        "scanner_version": "Version: 0.76.1",
        "database": {"UpdatedAt": now.isoformat()},
    }
    review = {
        "schema_version": "klip-host-risk-review.v1",
        "image": IMAGE,
        "source_commit": "a" * 40,
        "reviewed_at": "2026-11-04",
        "expires_at": "2026-11-11",
        "high_findings": [],
    }
    path, review_path = tmp_path / "image.json", tmp_path / "review.json"
    path.write_text(json.dumps(image))
    review_path.write_text(json.dumps(review))
    monkeypatch.setattr(security, "REVIEW", review_path)

    def save(document=report, header=summary):
        content = json.dumps(document).encode()
        (tmp_path / "scan.json").write_bytes(content)
        (tmp_path / "summary.json").write_text(
            json.dumps(
                {
                    **header,
                    "scan_sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        )

    save()
    return path, tmp_path, now, report, summary, save


def test_full_coverage_and_current_independent_review_pass(evidence):
    path, directory, now, *_ = evidence
    result = security.assess(path, directory, now=now)
    assert result["image"] == IMAGE and result["critical_findings"] == 0


def test_assessment_survives_saved_release_plan_json_roundtrip(evidence):
    path, directory, now, report, summary, save = evidence
    value = {
        "Severity": "HIGH",
        "VulnerabilityID": "synthetic-CVE",
        "PkgName": "fixture",
        "InstalledVersion": "1",
    }
    report["Results"][0]["Vulnerabilities"].append(value)
    save(report, summary)
    review = json.loads(security.REVIEW.read_text())
    review["high_findings"] = [
        {"finding": ["synthetic-CVE", "fixture", "1"], "rationale": "Synthetic runtime review"}
    ]
    security.REVIEW.write_text(json.dumps(review))
    result = security.assess(path, directory, now=now)
    assert result == json.loads(json.dumps(result))


@pytest.mark.parametrize(
    "change",
    ["critical", "new-high", "missing-coverage", "old-db", "old-scan", "other-image", "altered"],
)
def test_unreviewed_or_incomplete_scan_is_not_a_deployment_approval(evidence, change):
    path, directory, now, report, summary, save = evidence
    report, summary = copy.deepcopy(report), copy.deepcopy(summary)
    if change in {"critical", "new-high"}:
        report["Results"][0]["Vulnerabilities"].append(
            {
                "Severity": "CRITICAL" if change == "critical" else "HIGH",
                "VulnerabilityID": "synthetic-CVE",
                "PkgName": "fixture",
                "InstalledVersion": "1",
            }
        )
    if change == "missing-coverage":
        report["Results"].pop()
    if change == "old-db":
        summary["database"] = {"UpdatedAt": (now - timedelta(days=2)).isoformat()}
    if change == "old-scan":
        summary["scanned_at"] = (now - timedelta(days=2)).isoformat()
    if change == "other-image":
        report["ArtifactName"] = IMAGE + "-changed"
    save(report, summary)
    if change == "altered":
        (directory / "scan.json").write_text(json.dumps({**report, "changed": True}))
    with pytest.raises(ValueError):
        security.assess(path, directory, now=now)
