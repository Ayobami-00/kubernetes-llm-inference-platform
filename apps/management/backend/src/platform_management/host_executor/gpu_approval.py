"""An operator-staged runtime review gates candidate admission, never cleanup.

The private executor volume holds the cluster-bound receipt. Public dependency
reviews and artifact pins are versioned in the image, not supplied by a HostTask.
"""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

from common import ROOT
from gpu_operator import LOCK, settings

REVIEW = ROOT / "deploy/gpu-runtime-risk-review.json"
HOST_LOCK = ROOT / "deploy/gpu-host.lock.json"
BASE_LOCK = ROOT / "deploy/artifacts.lock.json"


def approval(root, cluster_uid, *, now=None):
    now = now or datetime.now(UTC)
    path = root / "cluster/gpu-runtime-approval.json"
    if path.is_symlink() or path.stat().st_size > 65536 or not path.is_file():
        raise ValueError("Missing or unsafe GPU runtime approval")
    value = json.loads(path.read_text())
    evidence = root / "cluster/gpu-runtime-assessment.json"
    if evidence.is_symlink() or not evidence.is_file() or evidence.stat().st_size > 262144:
        raise ValueError("Missing or unsafe GPU runtime assessment")
    body = evidence.read_bytes()
    assessment = json.loads(body)
    review = json.loads(REVIEW.read_text())
    start = datetime.fromisoformat(value["reviewed_at"])
    expiry = datetime.fromisoformat(value["expires_at"])
    if (
        value.get("schema_version") != "klip-gpu-runtime-approval.v1"
        or value.get("profile") != "single-a10"
        or value.get("kube_system_uid") != cluster_uid
        or value.get("review_sha256") != hashlib.sha256(REVIEW.read_bytes()).hexdigest()
        or value.get("operator_lock_sha256") != hashlib.sha256(LOCK.read_bytes()).hexdigest()
        or value.get("host_lock_sha256") != hashlib.sha256(HOST_LOCK.read_bytes()).hexdigest()
        or value.get("base_artifacts_sha256") != hashlib.sha256(BASE_LOCK.read_bytes()).hexdigest()
        or value.get("images") != settings()["images"]
        or not re.fullmatch(r"[a-f0-9]{64}", value.get("assessment_sha256", ""))
        or value["assessment_sha256"] != hashlib.sha256(body).hexdigest()
        or assessment.get("schema_version") != "klip-gpu-runtime-assessment.v1"
        or assessment.get("status") != "conditionally-reviewed-single-a10-bootstrap-only"
        or assessment.get("profile") != "single-a10"
        or assessment.get("review_sha256") != value["review_sha256"]
        or assessment.get("configuration") != review.get("configuration")
        or assessment.get("expires_at") != value["expires_at"]
        or set(assessment.get("images", {})) != set(review.get("images", {}))
        or not start <= now < expiry
        or expiry - start > timedelta(days=7)
        or review.get("profile") != "single-a10"
        or review.get("reviewed_at") != value["reviewed_at"]
        or review.get("expires_at") != value["expires_at"]
        or not isinstance(value.get("operator"), dict)
        or set(value.get("operator", {})) != {"deployment_uid", "policy_uid", "policy_spec_sha256"}
        or any(
            not re.fullmatch(
                r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", value["operator"].get(k, "")
            )
            for k in ("deployment_uid", "policy_uid")
        )
        or not re.fullmatch(
            r"[a-f0-9]{64}", value.get("operator", {}).get("policy_spec_sha256", "")
        )
    ):
        raise ValueError("GPU runtime approval identity, pins, profile or expiry differs")
    return value


def configured(root, cluster_uid):
    try:
        value = approval(root, cluster_uid)
        return {
            "gpu_inspection_configured": True,
            "gpu_runtime_review_sha256": value["review_sha256"],
        }
    except (OSError, ValueError, KeyError, TypeError):
        return {"gpu_inspection_configured": False, "gpu_runtime_review_sha256": None}
