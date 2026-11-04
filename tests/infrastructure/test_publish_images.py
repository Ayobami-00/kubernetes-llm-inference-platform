"""Image retries preserve immutable tags and scans cannot imply an approval."""

import json
from subprocess import CompletedProcess

import publish_images as images
import pytest

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


def test_repeated_resolution_reuses_one_exact_digest(monkeypatch):
    monkeypatch.setattr(images, "aws", lambda *a, **k: {"imageDetails": [{"imageDigest": DIGEST}]})
    assert images.resolve("stub", SHA) == images.resolve("stub", SHA)
    assert images.resolve("stub", SHA)["repository"] == "klip/workload-runner"
    assert images.resolve("stub", SHA)["tag"] != images.resolve("runner", SHA)["tag"]
    with pytest.raises(ValueError, match="exact source"):
        images.identity("runner", "main")


def test_model_build_uses_its_separate_retained_repository():
    assert images.identity("model-serving", SHA) == ("klip/model-serving", "model-serving-" + SHA)


def test_only_image_not_found_allows_a_new_publication(monkeypatch):
    monkeypatch.setattr(
        images.subprocess,
        "run",
        lambda *a, **k: CompletedProcess([], 1, "", "AccessDeniedException"),
    )
    with pytest.raises(RuntimeError, match="withheld"):
        images.resolve("backend", SHA)
    monkeypatch.setattr(
        images.subprocess,
        "run",
        lambda *a, **k: CompletedProcess([], 1, "", "ImageNotFoundException"),
    )
    assert images.resolve("backend", SHA)["digest"] is None


def test_scan_receipt_keeps_findings_and_frozen_build_identity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "uv.lock").write_text("test lock")
    recipe = tmp_path / images.COMPONENTS["backend"][1]
    recipe.parent.mkdir(parents=True)
    recipe.write_text("FROM test@sha256:test")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    scans = iter(
        [
            {"imageScanStatus": {"status": "PENDING"}},
            {
                "imageScanStatus": {"status": "COMPLETE"},
                "imageScanFindings": {"findingSeverityCounts": {"HIGH": 3}},
            },
        ]
    )

    def aws(action, *args, **kwargs):
        if action == "describe-images":
            return {"imageDetails": [{"imageDigest": DIGEST}]}
        return next(scans)

    monkeypatch.setattr(images, "aws", aws)
    sleeps = []
    receipt = json.loads(images.record("backend", SHA, sleep=sleeps.append).read_text())
    assert receipt["pinned_image"].endswith("@" + DIGEST)
    assert receipt["scan"]["imageScanFindings"]["findingSeverityCounts"] == {"HIGH": 3}
    assert receipt["limitations"] == [
        "A completed scan is evidence, not a deployment security approval"
    ]
    assert sleeps == [10]
    assert receipt["workflow_run_attempt"] == "2"


def test_incomplete_scan_cannot_emit_a_success_receipt(monkeypatch):
    def aws(action, *args, **kwargs):
        if action == "describe-images":
            return {"imageDetails": [{"imageDigest": DIGEST}]}
        return {"imageScanStatus": {"status": "UNSUPPORTED_IMAGE"}}

    monkeypatch.setattr(images, "aws", aws)
    with pytest.raises(ValueError, match="UNSUPPORTED_IMAGE"):
        images.record("backend", SHA, sleep=lambda _: None)
