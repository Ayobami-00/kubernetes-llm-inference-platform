"""Native evidence is synthesized here; these fixtures never certify a real image."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
import retained_gateway


def test_private_gateway_review_rejects_rebound_runtime_findings(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    (tmp_path / "deploy").mkdir()
    (tmp_path / "scan").mkdir()
    chart = tmp_path / "router.tgz"
    chart.write_bytes(b"synthetic-chart")
    config = tmp_path / "gateway.yaml"
    config.write_text("synthetic-private-listener")
    monkeypatch.setattr(retained_gateway, "ROOT", tmp_path)
    monkeypatch.setattr(retained_gateway, "CHART", chart)
    monkeypatch.setattr(retained_gateway, "CHART_SHA", retained_gateway.sha(chart))
    report = {
        "ArtifactName": "synthetic@sha256:" + "a" * 64,
        "Metadata": {"ImageConfig": {"architecture": "amd64"}},
        "Results": [{"Vulnerabilities": []}],
    }
    summary = {"image": report["ArtifactName"], "completed_at": now.isoformat()}
    review = {
        "scope": "private-authenticated-writerassist-experiment",
        "reviewed_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "configuration": {"gateway.yaml": retained_gateway.sha(config)},
        "components": {
            "proxy": {
                "image": report["ArtifactName"],
                "directory": "scan",
                "report_sha256": "",
                "high_findings": [],
            }
        },
    }

    def save():
        content = json.dumps(report).encode()
        (tmp_path / "scan/scan.json").write_bytes(content)
        (tmp_path / "scan/summary.json").write_text(json.dumps(summary))
        review["components"]["proxy"]["report_sha256"] = hashlib.sha256(content).hexdigest()
        (tmp_path / "deploy/retained-a10-gateway-risk-review.json").write_text(json.dumps(review))

    save()
    assert retained_gateway.review()["scope"] == review["scope"]
    report["Results"][0]["Vulnerabilities"] = [{"Severity": "CRITICAL"}]
    save()  # Rebinding the checksum cannot authorize a runtime Critical finding.
    with pytest.raises(ValueError, match="Unreviewed"):
        retained_gateway.review()
    report["Results"][0]["Vulnerabilities"] = []
    save()
    config.write_text("synthetic-public-listener")
    with pytest.raises(ValueError, match="configuration"):
        retained_gateway.review()
