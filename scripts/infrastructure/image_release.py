"""Assess exact CI image receipts and enforce the declared runtime boundaries."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from common import ROOT
from publish_images import COMPONENTS, REGISTRY

REVIEW = ROOT / "deploy/image-risk-review.json"


def assess(receipts: Path, commit: str, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    review = json.loads(REVIEW.read_text())
    if (
        not date.fromisoformat(review["reviewed_at"])
        <= now.date()
        < date.fromisoformat(review["expires_at"])
    ):
        raise ValueError("Runtime vulnerability review expired or is not yet effective")
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Exact build source commit required")
    resolved = {}
    for component, (repository, _) in COMPONENTS.items():
        paths = list(receipts.rglob(component + ".json"))
        if len(paths) != 1:
            raise ValueError("Exactly one CI image receipt is required for " + component)
        data = json.loads(paths[0].read_text())
        digest = data.get("digest", "")
        expected = f"{REGISTRY}/klip/{repository}@{digest}"
        if (
            data.get("schema_version") != "klip-private-image.v1"
            or data.get("source_commit") != commit
            or data.get("component") != component
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            or data.get("pinned_image") != expected
        ):
            raise ValueError("Image receipt identity/source/digest differs")
        scan = data["scan"]
        details = scan.get("imageScanFindings", {})
        observed = datetime.fromisoformat(details["imageScanCompletedAt"].replace("Z", "+00:00"))
        if (
            scan.get("imageScanStatus", {}).get("status") != "COMPLETE"
            or not timedelta(0) <= now - observed <= timedelta(hours=24)
            or scan.get("imageId", {}).get("imageDigest") != digest
        ):
            raise ValueError("Missing, stale or mismatched complete image scan")
        counts = details.get("findingSeverityCounts", {})
        if any(
            len([f for f in details.get("findings", []) if f["severity"] == severity])
            != counts.get(severity, 0)
            for severity in ("HIGH", "CRITICAL")
        ):
            raise ValueError("High/critical finding coverage is incomplete")
        if counts.get("CRITICAL", 0):
            raise ValueError("Critical image findings prohibit deployment")
        high = [f for f in details.get("findings", []) if f["severity"] == "HIGH"]
        if len(high) != counts.get("HIGH", 0):
            raise ValueError("High finding coverage is incomplete")
        residual = []
        for finding in high:
            attrs = {a["key"]: a["value"] for a in finding["attributes"]}
            matches = [
                r
                for r in review["findings"]
                if finding["name"] in r["ids"]
                and component in r["components"]
                and attrs.get("package_name") == r["package"]
                and attrs.get("package_version") in r["versions"]
            ]
            if len(matches) != 1:
                raise ValueError("Unreviewed high finding or changed package: " + finding["name"])
            residual.append(
                {
                    "id": finding["name"],
                    "severity": "HIGH",
                    "package": attrs["package_name"],
                    "version": attrs["package_version"],
                    "condition": review["conditions"][matches[0]["package"]],
                    "status": "requires-declared-runtime-controls",
                }
            )
        resolved[component] = {
            "image": expected,
            "scan_counts": counts,
            "receipt_sha256": hashlib.sha256(paths[0].read_bytes()).hexdigest(),
            "residual_findings": residual,
        }
    return {
        "source_commit": commit,
        "images": resolved,
        "review_expires_at": review["expires_at"],
        "review_sha256": hashlib.sha256(REVIEW.read_bytes()).hexdigest(),
    }


def guard_workloads(documents: list[dict]) -> None:
    """Fail before deployment if a chart weakens the conditions supporting review."""
    for doc in documents:
        if doc["kind"] not in {"Deployment", "Job"}:
            continue
        pod = doc["spec"]["template"]["spec"]
        context = pod.get("securityContext", {})
        if (
            not context.get("runAsNonRoot")
            or context.get("runAsUser", 0) == 0
            or context.get("seccompProfile", {}).get("type") != "RuntimeDefault"
            or any(pod.get(k) for k in ("hostNetwork", "hostPID", "hostIPC"))
            or any("hostPath" in v for v in pod.get("volumes", []))
        ):
            raise ValueError("Runtime image review requires isolated non-root workloads")
        for container in pod["containers"]:
            security = container.get("securityContext", {})
            if (
                security.get("privileged")
                or container.get("volumeDevices")
                or security.get("allowPrivilegeEscalation") is not False
                or security.get("capabilities", {}).get("drop") != ["ALL"]
                or security.get("capabilities", {}).get("add")
            ):
                raise ValueError("Runtime image review prohibits privilege escalation/capabilities")
            env = {v["name"]: v.get("value") for v in container.get("env", [])}
            if container["name"] != "web" and (
                env.get("LC_ALL") != "C.UTF-8" or env.get("LANGUAGE") != "C"
            ):
                raise ValueError("Runtime image review requires a fixed C locale")
