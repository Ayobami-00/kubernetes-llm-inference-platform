"""Private controller reviews reject stale scans, changed images and unreviewed vulnerabilities."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import inference_platform
import inference_security as security
import pytest
from inference_artifacts import settings


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    stamp = datetime.now(UTC)
    configuration = {"synthetic": "configuration-hash"}
    monkeypatch.setattr(inference_platform, "configuration_hashes", lambda: configuration)
    review = {
        "schema_version": "klip-inference-controller-review.v1",
        "scope": "private-controllers-only",
        "reviewed_at": stamp.isoformat(),
        "expires_at": (stamp + timedelta(days=7)).isoformat(),
        "configuration": configuration,
        "conditions": {"private": "Synthetic test boundary"},
        "components": {},
    }
    for name in security.COMPONENTS:
        item = settings()["images"][settings()["scan_components"][name]]
        image = item["reference"] + "@" + item["digest"]
        directory = tmp_path / name
        directory.mkdir()
        report = {
            "ArtifactName": image,
            "Metadata": {"ImageConfig": {"architecture": "amd64"}},
            "Results": [
                {
                    "Type": "gobinary",
                    "Packages": [{"Name": "synthetic-package"}],
                    "Vulnerabilities": [],
                }
            ],
        }
        path = directory / "scan.json"
        path.write_text(json.dumps(report))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        (directory / "summary.json").write_text(
            json.dumps(
                {
                    "schema_version": "klip-public-inference-scan.v1",
                    "status": "scanned",
                    "image": image,
                    "report_sha256": digest,
                    "scanner_version": "Version: 0.76.1",
                    "completed_at": stamp.isoformat(),
                    "database": {"UpdatedAt": stamp.isoformat()},
                }
            )
        )
        review["components"][name] = {
            "directory": name,
            "image": image,
            "report_sha256": digest,
            "high_findings": [],
        }
    path = tmp_path / "review.json"
    path.write_text(json.dumps(review))
    monkeypatch.setattr(security, "REVIEW", path)
    return tmp_path, stamp, review


def test_exact_fresh_private_controller_evidence_is_scoped(evidence):
    root, stamp, _ = evidence
    result = security.assess(root, now=stamp)
    assert result["scope"] == "private-controllers-only"
    assert set(result["components"]) == security.COMPONENTS


@pytest.mark.parametrize(
    "change", ["critical", "high", "image", "tamper", "stale", "scope", "expiry"]
)
def test_changed_or_incomplete_controller_evidence_cannot_authorize_rollout(evidence, change):
    root, stamp, review = evidence
    component = next(iter(review["components"]))
    directory = root / component
    path = directory / "scan.json"
    report = json.loads(path.read_text())
    summary_path = directory / "summary.json"
    summary = json.loads(summary_path.read_text())
    if change in {"critical", "high"}:
        report["Results"][0]["Vulnerabilities"] = [
            {
                "Severity": change.upper(),
                "VulnerabilityID": "SYNTHETIC-UNREVIEWED",
                "PkgName": "test",
                "InstalledVersion": "1",
            }
        ]
        path.write_text(json.dumps(report))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        summary["report_sha256"] = review["components"][component]["report_sha256"] = digest
    elif change == "image":
        review["components"][component]["image"] = "docker.io/unreviewed/image@sha256:" + "a" * 64
    elif change == "tamper":
        path.write_text(path.read_text() + " ")
    elif change == "stale":
        summary["database"]["UpdatedAt"] = (stamp - timedelta(hours=25)).isoformat()
    elif change == "scope":
        review["scope"] = "public-serving"
    else:
        review["expires_at"] = (stamp + timedelta(days=8)).isoformat()
    summary_path.write_text(json.dumps(summary))
    (root / "review.json").write_text(json.dumps(review))
    with pytest.raises(ValueError):
        security.assess(root, now=stamp)
