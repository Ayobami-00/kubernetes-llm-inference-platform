"""Approval staging freezes source/custody before a single, observable write attempt."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import gpu_runtime_approval as staging
import pytest


@pytest.mark.parametrize("changed", [None, "source", "custody", "response"])
def test_prepare_apply_freezes_the_real_helper_source(tmp_path, monkeypatch, changed):
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps(
            {
                "reviewed_at": datetime.now(UTC).isoformat(),
                "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            }
        )
    )
    monkeypatch.setattr(staging, "REVIEW", review)
    monkeypatch.setattr(staging, "folder", lambda: tmp_path / "plans")
    monkeypatch.setattr(staging, "assess", lambda *a: {"synthetic": True})
    identity = {
        "identities": {"/namespace/kube-system": "synthetic-cluster"},
        "image": "synthetic-image",
        "operator": {"policy_uid": "synthetic-policy"},
    }
    calls = []

    def run(args, *, data, **kwargs):
        calls.append(args)
        value = json.loads(data)
        assert (tmp_path / "plans" / staging.digest(value) / "execution.json").is_file()
        if changed == "response":
            raise TimeoutError("synthetic transport loss")
        return json.dumps(
            {
                "status": "staged",
                **{k: value["approval"][k] for k in ("review_sha256", "assessment_sha256")},
            }
        )

    monkeypatch.setattr(
        staging,
        "observe_cluster",
        lambda: (SimpleNamespace(run=run), "synthetic-native-pod", identity),
    )
    prepared = staging.prepare(tmp_path / "images", tmp_path / "host")
    key = prepared["plan_sha256"]
    assert prepared["gpu_instances"] == 0 and prepared["host_mutations"] == 0
    if changed == "source":
        review.write_text(review.read_text() + " ")
    elif changed == "custody":
        # Prepare captured data rather than retaining a mutable Python reference.
        identity["image"] = "foreign-image"
    if changed:
        with pytest.raises((ValueError, TimeoutError)):
            staging.apply(key)
        assert len(calls) == (1 if changed == "response" else 0)
        if changed == "response":
            value = json.loads((tmp_path / "plans" / key / "execution.json").read_text())
            assert value["status"] == "reconciliation_required"
    else:
        assert staging.apply(key)["status"] == "staged"
        with pytest.raises(ValueError, match="original attempt"):
            staging.apply(key)
        assert len(calls) == 1
