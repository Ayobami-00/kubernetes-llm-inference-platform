"""A changed image, finding, runtime boundary or expired review blocks rollout."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import observability_security as security
import pytest


def fixture(tmp_path, monkeypatch):
    now = datetime(2026, 11, 3, 22, tzinfo=UTC)
    reference = "example.invalid/synthetic-monitor:v1"
    pinned = reference + "@sha256:" + "a" * 64
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"images": {reference: {}}}))
    monkeypatch.setattr(security, "LOCK", lock)
    monkeypatch.setattr(security, "image", lambda _: pinned)
    monkeypatch.setattr(security, "configuration_hashes", lambda: {"boundary": "reviewed"})
    report = {
        "ArtifactName": pinned,
        "Results": [
            {
                "Type": "gobinary",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "SYNTHETIC-TEST-1",
                        "PkgName": "synthetic-package",
                        "InstalledVersion": "v1",
                        "Severity": "HIGH",
                    }
                ],
            }
        ],
    }
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    review = {
        "reviewed_at": (now - timedelta(hours=1)).isoformat(),
        "expires_at": (now + timedelta(days=1)).isoformat(),
        "configuration": {"boundary": "reviewed"},
        "scan_source_commit": "source",
        "conditions": {"restricted": "Synthetic runtime control for test only"},
        "images": {
            pinned: {
                "report_file": "report.json",
                "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
                "high_findings": [
                    {
                        "id": "SYNTHETIC-TEST-1",
                        "package": "synthetic-package",
                        "version": "v1",
                        "condition": "restricted",
                    }
                ],
            }
        },
    }
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review))
    monkeypatch.setattr(security, "REVIEW", review_path)
    (tmp_path / "summary.json").write_text(
        json.dumps(
            {
                "source_commit": "source",
                "completed_at": (now - timedelta(minutes=15)).isoformat(),
                "scanner_version": "Version: 0.76.1",
            }
        )
    )
    return now, pinned, report, review, report_path, review_path


def test_exact_findings_are_conditional_not_declared_fixed(tmp_path, monkeypatch):
    now, *_ = fixture(tmp_path, monkeypatch)
    observed = security.assess(tmp_path, now=now)
    assert observed["status"] == "conditionally-reviewed"
    assert next(iter(observed["images"].values()))["high_findings"] == 1


def test_expired_changed_boundary_and_changed_bytes_are_rejected(tmp_path, monkeypatch):
    now, _, _, _, report, _ = fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="expired"):
        security.assess(tmp_path, now=now + timedelta(days=2))
    monkeypatch.setattr(security, "configuration_hashes", lambda: {"boundary": "public"})
    with pytest.raises(ValueError, match="runtime boundary"):
        security.assess(tmp_path, now=now)
    monkeypatch.setattr(security, "configuration_hashes", lambda: {"boundary": "reviewed"})
    report.write_text(report.read_text() + " ")
    with pytest.raises(ValueError, match="checksum"):
        security.assess(tmp_path, now=now)


@pytest.mark.parametrize("severity,reason", [("HIGH", "High"), ("CRITICAL", "Critical")])
def test_new_findings_are_not_waived_by_package_name(tmp_path, monkeypatch, severity, reason):
    now, pinned, report, review, report_path, review_path = fixture(tmp_path, monkeypatch)
    finding = report["Results"][0]["Vulnerabilities"][0]
    finding.update(VulnerabilityID="SYNTHETIC-UNREVIEWED", Severity=severity)
    report_path.write_text(json.dumps(report))
    review["images"][pinned]["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    review_path.write_text(json.dumps(review))
    with pytest.raises(ValueError, match=reason):
        security.assess(tmp_path, now=now)
