"""Synthetic scan data checks fail closed on missing coverage and runtime drift."""

import json
from datetime import UTC, datetime

import pytest
from image_release import assess, guard_workloads
from publish_images import COMPONENTS, REGISTRY

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
NOW = datetime(2026, 11, 3, 18, tzinfo=UTC)


def receipts(root, *, critical=0, high=None):
    for component, (repo, _) in COMPONENTS.items():
        findings = [high] if high and component == "backend" else []
        counts = {"CRITICAL": critical, "HIGH": len(findings)}
        findings += [{"name": "synthetic-critical", "severity": "CRITICAL"}] * critical
        data = {
            "schema_version": "klip-private-image.v1",
            "component": component,
            "source_commit": SHA,
            "digest": DIGEST,
            "pinned_image": f"{REGISTRY}/klip/{repo}@{DIGEST}",
            "scan": {
                "imageScanStatus": {"status": "COMPLETE"},
                "imageId": {"imageDigest": DIGEST},
                "imageScanFindings": {
                    "imageScanCompletedAt": NOW.isoformat(),
                    "findingSeverityCounts": counts,
                    "findings": findings,
                },
            },
        }
        (root / (component + ".json")).write_text(json.dumps(data))


def finding(identity="CVE-2026-84243", version="2.36-9+deb12u14"):
    return {
        "name": identity,
        "severity": "HIGH",
        "attributes": [
            {"key": "package_name", "value": "glibc"},
            {"key": "package_version", "value": version},
        ],
    }


def test_known_residuals_remain_visible_and_time_bounded(tmp_path):
    receipts(tmp_path, high=finding())
    report = assess(tmp_path, SHA, now=NOW)
    assert report["images"]["backend"]["scan_counts"]["HIGH"] == 1
    assert (
        report["images"]["backend"]["residual_findings"][0]["status"]
        == "requires-declared-runtime-controls"
    )
    with pytest.raises(ValueError, match="expired"):
        assess(tmp_path, SHA, now=datetime(2026, 12, 4, tzinfo=UTC))


def test_critical_and_changed_high_findings_prohibit_deployment(tmp_path):
    receipts(tmp_path, critical=1)
    with pytest.raises(ValueError, match="Critical"):
        assess(tmp_path, SHA, now=NOW)
    receipts(tmp_path, high=finding(version="different-package-build"))
    with pytest.raises(ValueError, match="Unreviewed"):
        assess(tmp_path, SHA, now=NOW)


def test_wrong_source_and_stale_or_incomplete_scans_cannot_pass(tmp_path):
    receipts(tmp_path)
    with pytest.raises(ValueError, match="identity/source"):
        assess(tmp_path, "c" * 40, now=NOW)
    with pytest.raises(ValueError, match="stale"):
        assess(tmp_path, SHA, now=datetime(2026, 11, 5, tzinfo=UTC))
    p = tmp_path / "backend.json"
    data = json.loads(p.read_text())
    data["scan"]["imageScanFindings"]["findingSeverityCounts"]["HIGH"] = 1
    p.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="coverage"):
        assess(tmp_path, SHA, now=NOW)


def test_declared_controls_reject_privileged_workloads():
    pod = {
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10001,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": "web",
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                },
            }
        ],
    }
    documents = [{"kind": "Deployment", "spec": {"template": {"spec": pod}}}]
    guard_workloads(documents)
    pod["containers"][0]["securityContext"]["capabilities"]["add"] = ["SYS_ADMIN"]
    with pytest.raises(ValueError, match="privilege"):
        guard_workloads(documents)
