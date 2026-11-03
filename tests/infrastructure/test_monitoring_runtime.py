"""Monitoring ownership and immutable plans remain fail-closed across retries."""

import copy
import json
from unittest.mock import Mock

import monitoring_runtime as runtime
import platform_resize
import pytest


def test_crd_defaults_are_accepted_but_schema_drift_and_adoption_are_not():
    desired = {
        "metadata": {"name": "synthetic.monitoring.coreos.com"},
        "spec": {"group": "monitoring.coreos.com", "versions": [{"name": "v1"}]},
    }
    actual = copy.deepcopy(desired)
    actual["metadata"]["labels"] = {"klip.dev/managed-by": "klip-monitoring-crds"}
    actual["spec"].update(conversion={"strategy": "None"}, preserveUnknownFields=False)
    runtime.check_crd(actual, desired)
    actual["spec"]["versions"][0]["name"] = "v2"
    with pytest.raises(ValueError, match="schema differs"):
        runtime.check_crd(actual, desired)
    actual["spec"]["versions"][0]["name"] = "v1"
    actual["metadata"]["labels"] = {}
    with pytest.raises(ValueError, match="ownership"):
        runtime.check_crd(actual, desired)


def test_terminal_apply_reuses_receipt_and_interrupted_install_never_replays(monkeypatch, tmp_path):
    plan = {"source_sha256": "original-source", "status": "prepared"}
    approval = runtime.sha(plan)
    plan["approval_sha256"] = approval
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    active = {"approval_sha256": approval, "status": "installed"}
    path = tmp_path / "active.json"
    path.write_text(json.dumps(active))
    monkeypatch.setattr(runtime, "folder", lambda: tmp_path)
    readiness = Mock(side_effect=AssertionError("Terminal reuse must not launch again"))
    monkeypatch.setattr(platform_resize, "verify_capacity", readiness)
    assert runtime.apply(approval) == active
    active["status"] = "interrupted"
    path.write_text(json.dumps(active))
    with pytest.raises(ValueError, match="no automatic replay"):
        runtime.apply(approval)
    readiness.assert_not_called()
    plan["source_sha256"] = "modified"
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="exact reviewed"):
        runtime.apply(approval)


def test_new_plan_cannot_overwrite_an_ambiguous_install(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "folder", lambda: tmp_path)
    (tmp_path / "active.json").write_text(json.dumps({"status": "installing"}))
    with pytest.raises(ValueError, match="original interrupted"):
        runtime.prepare(tmp_path)
