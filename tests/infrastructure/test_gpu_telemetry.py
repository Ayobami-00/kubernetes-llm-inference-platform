"""A telemetry update cannot smuggle an image, placement or broad-egress change."""

import copy
import json

import gpu_operator as gpu
import gpu_telemetry as telemetry
import pytest
import yaml


def previous():
    values = copy.deepcopy(gpu.values())
    values["dcgmExporter"]["serviceMonitor"].pop("additionalLabels")
    boundary = list(yaml.safe_load_all(gpu.boundary()))[:-1]
    policy = {
        "kind": "ClusterPolicy",
        "spec": {"dcgmExporter": {"serviceMonitor": {"additionalLabels": {}}}},
    }
    return values, boundary, [policy]


def test_update_permits_exact_discovery_label_and_egress_only(monkeypatch):
    old_values, old_boundary, old_rendered = previous()
    new = copy.deepcopy(old_rendered)
    new[0]["spec"]["dcgmExporter"]["serviceMonitor"]["additionalLabels"] = {
        "release": "klip-monitoring"
    }
    monkeypatch.setattr(telemetry, "render", lambda: yaml.safe_dump_all(new))
    policy = telemetry.validate_delta(
        yaml.safe_dump(old_values),
        yaml.safe_dump_all(old_boundary),
        yaml.safe_dump_all(old_rendered),
    )
    assert policy["metadata"]["namespace"] == "observability"
    assert policy["spec"]["egress"][0]["toPorts"] == [
        {"ports": [{"port": "9400", "protocol": "TCP"}]}
    ]
    new[0]["spec"]["driver"] = {"enabled": False}
    with pytest.raises(ValueError, match="chart delta"):
        telemetry.validate_delta(
            yaml.safe_dump(old_values),
            yaml.safe_dump_all(old_boundary),
            yaml.safe_dump_all(old_rendered),
        )


@pytest.mark.parametrize("changed", ["image", "placement", "egress"])
def test_update_refuses_unrelated_existing_configuration(changed):
    values, boundary, rendered = previous()
    if changed == "image":
        values["operator"]["version"] = "latest"
    elif changed == "placement":
        values["operator"]["nodeSelector"] = {}
    else:
        boundary[1]["spec"]["egress"].append({"toEntities": ["world"]})
    with pytest.raises(ValueError):
        telemetry.validate_delta(
            yaml.safe_dump(values), yaml.safe_dump_all(boundary), yaml.safe_dump_all(rendered)
        )


@pytest.mark.parametrize(
    "changed", ["none", "node", "gpu", "selector", "namespace", "labels", "already-created"]
)
def test_live_discovery_and_identity_must_match(changed):
    nodes = [{"metadata": {"name": "platform-01", "uid": "node", "labels": {}}, "status": {}}]
    prometheus = {
        "metadata": {"uid": "prom"},
        "spec": {
            "serviceMonitorSelector": {"matchLabels": {"release": "klip-monitoring"}},
            "serviceMonitorNamespaceSelector": {},
        },
    }
    policy = {
        "metadata": {"uid": "policy"},
        "spec": {"dcgmExporter": {"serviceMonitor": {"additionalLabels": {}}}},
    }
    if changed == "node":
        nodes[0]["metadata"]["uid"] = "replacement"
    elif changed == "gpu":
        nodes[0]["metadata"]["labels"] = gpu.CANDIDATE
    elif changed == "selector":
        prometheus["spec"]["serviceMonitorSelector"] = {}
    elif changed == "namespace":
        prometheus["spec"].pop("serviceMonitorNamespaceSelector")
    elif changed == "labels":
        policy["spec"]["dcgmExporter"]["serviceMonitor"]["additionalLabels"] = {"release": "other"}

    def kubectl(*args):
        if args[:2] == ("get", "nodes"):
            return json.dumps({"items": nodes})
        if "prometheus" in args:
            return json.dumps({"items": [prometheus]})
        if "clusterpolicy" in args:
            return json.dumps(policy)
        if "--ignore-not-found" in args:
            return "existing" if changed == "already-created" else ""
        raise AssertionError(args)

    active = {"cluster": {"nodes": {"platform-01": "node"}}}
    if changed == "none":
        assert telemetry.current(kubectl, active)["prometheus_uid"] == "prom"
    else:
        with pytest.raises(ValueError):
            telemetry.current(kubectl, active)


@pytest.mark.skipif(
    not (gpu.CACHE / gpu.RELEASE / "Chart.yaml").exists(), reason="Verified charts required"
)
def test_actual_chart_delta_is_only_the_discovery_label(monkeypatch):
    old_values = gpu.values()
    old_values["dcgmExporter"]["serviceMonitor"].pop("additionalLabels")
    with monkeypatch.context() as original:
        original.setattr(gpu, "values", lambda: old_values)
        old_rendered = gpu.render()
    old_boundary = yaml.safe_dump_all(list(yaml.safe_load_all(gpu.boundary()))[:-1])
    assert telemetry.validate_delta(yaml.safe_dump(old_values), old_boundary, old_rendered)
