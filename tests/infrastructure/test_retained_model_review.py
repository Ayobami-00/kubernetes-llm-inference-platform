"""Synthetic evidence tests the narrow review; it never claims a real scan passed."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import retained_model_review as review_module


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    root = tmp_path / "source"
    (root / "deploy").mkdir(parents=True)
    scan = tmp_path / "scan"
    scan.mkdir()
    monkeypatch.setattr(review_module, "ROOT", root)
    image = "test/model@sha256:" + "a" * 64
    report = {
        "ArtifactName": image,
        "Metadata": {"ImageConfig": {"architecture": "amd64"}},
        "Results": [
            {
                "Vulnerabilities": [
                    {
                        "Severity": "CRITICAL",
                        "PkgName": "linux-libc-dev",
                        "InstalledVersion": "6.8.0-147.147",
                        "VulnerabilityID": f"SYNTHETIC-{i}",
                    }
                    for i in range(15)
                ]
            }
        ],
    }
    headers = {
        "package": "linux-libc-dev",
        "version": "6.8.0-147.147",
        "files": [
            {"path": f"/usr/include/synthetic-{i}.h", "kind": "file", "mode": 0o644}
            for i in range(990)
        ],
    }
    summary = {
        "image": image,
        "scanner_version": "Version: 0.76.1",
        "observed_at": now.isoformat(),
        "database": {"UpdatedAt": now.isoformat()},
    }
    review = {
        "schema_version": "klip-retained-a10-model-review.v1",
        "scope": "private-authenticated-writerassist-experiment",
        "instance_id": "8f2e29b7279b42148ffaaed4a4dcdd8e",
        "reviewed_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(hours=23)).isoformat(),
        "image": image,
        "runtime_high_findings": [],
        "conditions": {
            k: "Synthetic test condition"
            for k in ("kernel_headers", "fixed_inputs", "no_asn1_input", "brotli", "isolation")
        },
        "limitations": ["Synthetic test metadata; no real image scan."],
    }

    def save():
        for name, body in [
            ("scan.json", report),
            ("header-inventory.json", headers),
            ("patch-receipt.json", {"synthetic": True}),
        ]:
            (scan / name).write_text(json.dumps(body))
        for name, key in [
            ("scan.json", "report_sha256"),
            ("header-inventory.json", "header_inventory_sha256"),
            ("patch-receipt.json", "patch_receipt_sha256"),
        ]:
            summary[key] = review[key] = review_module.digest(scan / name)
        (scan / "summary.json").write_text(json.dumps(summary))
        (root / "deploy/retained-a10-model-risk-review.json").write_text(json.dumps(review))

    save()
    return scan, now, report, headers, review, save


def test_header_applicability_is_not_a_clean_scan(evidence):
    scan, now, *_ = evidence
    result = review_module.assess(scan, now=now)
    assert result["runtime_critical_findings"] == 0
    assert result["critical_header_findings"] == 15
    assert result["scope"] == "private-authenticated-writerassist-experiment"


@pytest.mark.parametrize(
    "change", ["runtime-critical", "executable-header", "other-image", "expired", "unreviewed-high"]
)
def test_review_cannot_hide_runtime_findings_or_different_evidence(evidence, change):
    scan, now, report, headers, review, save = evidence
    if change == "runtime-critical":
        report["Results"][0]["Vulnerabilities"][0]["PkgName"] = "openssl"
    elif change == "executable-header":
        headers["files"][0]["mode"] = 0o755
    elif change == "other-image":
        report["ArtifactName"] = "other@sha256:" + "b" * 64
    elif change == "expired":
        review["expires_at"] = (now - timedelta(seconds=1)).isoformat()
    else:
        report["Results"][0]["Vulnerabilities"].append(
            {
                "Severity": "HIGH",
                "PkgName": "runtime",
                "InstalledVersion": "1",
                "VulnerabilityID": "SYNTHETIC-NEW",
            }
        )
    # Even a checksum-rebound fixture must fail the independent applicability checks.
    save()
    with pytest.raises(ValueError):
        review_module.assess(scan, now=now)
