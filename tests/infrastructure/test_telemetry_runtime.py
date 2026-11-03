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
