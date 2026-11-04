"""Changed artifacts, freshness or unreviewed findings cannot reuse a GPU review."""

import json
from datetime import UTC, datetime, timedelta

import gpu_operator_security as security
import pytest
from gpu_operator import image, sha

NOW = datetime(2026, 11, 4, 6, tzinfo=UTC)


@pytest.fixture
def review(tmp_path, monkeypatch):
    configuration = {"values": "bound"}
    monkeypatch.setattr(security, "configuration_hashes", lambda: configuration)
    monkeypatch.setattr(security, "REVIEW", tmp_path / "review.json")
    data = {
        "reviewed_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(days=7)).isoformat(),
        "configuration": configuration,
        "images": {},
    }
    for index, reference in enumerate(security.REFERENCES):
        filename = f"{index}.json"
        scan = {
            "ArtifactName": image(reference),
            "CreatedAt": NOW.isoformat(),
            "Trivy": {"Version": "0.76.1"},
            "Metadata": {"ImageConfig": {"architecture": "amd64", "os": "linux"}},
            "Results": [{"Type": "gobinary", "Vulnerabilities": []}],
        }
        body = json.dumps(scan).encode()
        (tmp_path / filename).write_bytes(body)
        data["images"][image(reference)] = {
            "report_file": filename,
            "report_sha256": sha(body),
            "high_findings": [],
        }
    security.REVIEW.write_text(json.dumps(data))
    (tmp_path / "scan-context.json").write_text(
        json.dumps(
            {
                "scanner_version": "Version: 0.76.1",
                "completed_at": NOW.isoformat(),
                "database": {"UpdatedAt": NOW.isoformat()},
            }
        )
    )
    return tmp_path, data


def test_review_binds_only_cpu_controller_images(review):
    directory, _ = review
    result = security.assess(directory, now=NOW)
    assert result["status"] == "conditionally-reviewed-cpu-controllers-only"
    assert set(result["images"]) == {image(r) for r in security.REFERENCES}


@pytest.mark.parametrize("hours", [-1, 25, 169])
def test_scan_freshness_and_review_expiry_are_enforced(review, hours):
    directory, _ = review
    with pytest.raises(ValueError):
        security.assess(directory, now=NOW + timedelta(hours=hours))


@pytest.mark.parametrize("change", ["critical", "high", "native-stale", "platform", "scanner"])
def test_changed_or_unreviewed_native_findings_fail_even_with_new_report_hash(review, change):
    directory, data = review
    scan_path = directory / "0.json"
    scan = json.loads(scan_path.read_text())
    if change in {"critical", "high"}:
        scan["Results"][0]["Vulnerabilities"] = [
            {
                "Severity": change.upper(),
                "VulnerabilityID": "CVE-test",
                "PkgName": "stdlib",
                "InstalledVersion": "v1",
            }
        ]
    elif change == "native-stale":
        scan["CreatedAt"] = (NOW - timedelta(hours=25)).isoformat()
    elif change == "platform":
        scan["Metadata"]["ImageConfig"]["architecture"] = "arm64"
    else:
        scan["Trivy"]["Version"] = "0.1"
    body = json.dumps(scan).encode()
    scan_path.write_bytes(body)
    data["images"][image(security.REFERENCES[0])]["report_sha256"] = sha(body)
    security.REVIEW.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        security.assess(directory, now=NOW)


def test_runtime_boundary_change_invalidates_review(review, monkeypatch):
    directory, _ = review
    monkeypatch.setattr(security, "configuration_hashes", lambda: {"values": "broader"})
    with pytest.raises(ValueError, match="runtime configuration"):
        security.assess(directory, now=NOW)
