"""Thanos plans retain fixed topology, storage scope and execution identity."""

import json
from unittest.mock import Mock

import pytest
import telemetry_runtime as runtime
import yaml
from observability_release import thanos_values


def test_thanos_dedup_and_retention_do_not_enable_other_ingestion_components():
    values = thanos_values()
    assert values["query"]["replicaLabels"] == ["prometheus_replica"]
    assert values["query"]["replicaCount"] == values["storegateway"]["replicaCount"] == 2
    assert values["compactor"]["replicaCount"] == 1
    assert set(values["compactor"]["retention"].values()) == {"14d"}
    for component in ("receive", "ruler", "queryFrontend", "rustfs", "kube-prometheus-stack"):
        assert not values[component]["enabled"]
    assert "@sha256:" in values["global"]["image"]["tag"]
    config = yaml.safe_load(values["global"]["objstore"]["config"])
    assert config["type"] == "S3" and config["config"]["aws_sdk_auth"]
    assert "access_key" not in config["config"] and "secret_key" not in config["config"]
    store = values["storegateway"]["serviceAccount"]
    compact = values["compactor"]["serviceAccount"]
    assert (
        store["annotations"]["eks.amazonaws.com/role-arn"]
        != compact["annotations"]["eks.amazonaws.com/role-arn"]
    )
    assert not values["global"]["serviceAccount"]["automountServiceAccountToken"]


def test_terminal_reuse_never_reinstalls_and_conflicting_values_are_rejected(monkeypatch, tmp_path):
    plan = {"component": "thanos", "values": {"synthetic": True}}
    approval = runtime.sha(plan)
    plan["approval_sha256"] = approval
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    active = {"approval_sha256": approval, "status": "installed"}
    (tmp_path / "active.json").write_text(json.dumps(active))
    monkeypatch.setattr(runtime, "folder", lambda: tmp_path)
    gate = Mock(side_effect=AssertionError("A terminal deployment must not replay Helm"))
    monkeypatch.setattr(runtime, "verify_roles", gate)
    assert runtime.apply(approval) == active
    gate.assert_not_called()
    plan["values"]["synthetic"] = False
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="exact prepared"):
        runtime.apply(approval)


@pytest.mark.parametrize("remaining", ["secrets", "workloads", None])
def test_failed_first_install_requires_observed_complete_rollback(monkeypatch, tmp_path, remaining):
    import cluster

    active = {
        "approval_sha256": "synthetic",
        "status": "interrupted",
        "interrupted_stage": "installing",
    }
    (tmp_path / "active.json").write_text(json.dumps(active))
    monkeypatch.setattr(runtime, "folder", lambda: tmp_path)
    monkeypatch.setattr(runtime, "retained_claims", lambda: {})

    def observe(*args):
        objects = remaining == ("secrets" if "secret" in args else "workloads")
        return json.dumps({"items": [{"synthetic": True}] if objects else []})

    monkeypatch.setattr(cluster, "kubectl", observe)
    if remaining:
        with pytest.raises(ValueError, match="ambiguous"):
            runtime.reconcile("synthetic")
        assert json.loads((tmp_path / "active.json").read_text()) == active
    else:
        result = runtime.reconcile("synthetic")
        assert result["status"] == "failed" and result["rollback_observed"]
        assert (tmp_path / "synthetic.result.json").exists()


@pytest.mark.parametrize("helm_started", [False, True])
def test_validation_failure_retires_only_before_helm_started(monkeypatch, tmp_path, helm_started):
    active = {
        "approval_sha256": "synthetic",
        "status": "interrupted",
        "interrupted_stage": "validating",
    }
    if helm_started:
        active["helm_started_at"] = "synthetic-start"
    (tmp_path / "active.json").write_text(json.dumps(active))
    monkeypatch.setattr(runtime, "folder", lambda: tmp_path)
    if helm_started:
        with pytest.raises(ValueError, match="separately observed"):
            runtime.reconcile("synthetic")
        assert json.loads((tmp_path / "active.json").read_text()) == active
    else:
        result = runtime.reconcile("synthetic")
        assert result["status"] == "failed"
        assert result["helm_not_started"] and result["prerequisite_resources_retained"]
        assert runtime.reconcile("synthetic") == result
