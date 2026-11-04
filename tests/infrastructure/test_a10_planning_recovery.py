"""A crash between immutable plan creation and database admission keeps the same inputs."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from platform_management.jobs import a10_experiment
from workload_runner.experiments.a10 import NODE


@pytest.fixture
def inputs():
    data = {
        "id": "experiment-test",
        "definition_sha256": "a" * 64,
        "definition": {"inventory_template": {"nodes": [{"id": NODE}]}},
    }
    evidence = {
        "node_uid": "gpu-node",
        "boot_id": "gpu-boot",
        "deployment_uid": "serving-deployment",
        "generation": 4,
        "pods": [{"name": "model-pod", "uid": "serving-pod", "ready": True}],
        "observed_at": "first-observation",
        "client_node": {
            "name": "platform-01",
            "uid": "cpu-node",
            "boot_id": "cpu-boot",
            "observed_at": "first-observation",
        },
    }
    return data, evidence


def test_recovery_before_child_commit_preserves_plan_bytes(tmp_path, monkeypatch, inputs):
    data, evidence = inputs
    clock = datetime(2026, 11, 4, tzinfo=UTC)
    monkeypatch.setattr(a10_experiment, "now", lambda: clock)
    first = a10_experiment.planning_inputs(data, "baseline", tmp_path, evidence)
    snapshot = json.dumps(first, sort_keys=True)
    saved = (tmp_path / "baseline/planning-inputs.json").read_bytes()
    clock += timedelta(seconds=20)
    evidence["observed_at"] = "new-observation"
    evidence["client_node"]["observed_at"] = "new-observation"
    recovered = a10_experiment.planning_inputs(data, "baseline", tmp_path, evidence)
    assert json.dumps(recovered, sort_keys=True) == snapshot
    assert (tmp_path / "baseline/planning-inputs.json").read_bytes() == saved
    assert data["definition"]["inventory_template"] == {"nodes": [{"id": NODE}]}


@pytest.mark.parametrize("change", ["definition", "variant", "pod", "cpu-boot", "checksum"])
def test_recovery_rejects_changed_ownership_or_measured_identity(tmp_path, inputs, change):
    data, evidence = inputs
    a10_experiment.planning_inputs(data, "baseline", tmp_path, evidence)
    candidate = copy.deepcopy(data)
    path = tmp_path / "baseline/planning-inputs.json"
    if change in {"definition", "variant", "checksum"}:
        stored = json.loads(path.read_text())
        if change == "definition":
            candidate["definition_sha256"] = "b" * 64
        elif change == "variant":
            stored["identity"]["variant"] = "ngram"
        else:
            stored["inputs"]["inventory"]["captured_at"] = "changed"
        path.write_text(json.dumps(stored))
    elif change == "pod":
        evidence["pods"][0]["uid"] = "replacement-pod"
    else:
        evidence["client_node"]["boot_id"] = "restarted-cpu"
    with pytest.raises(ValueError, match="changed"):
        a10_experiment.planning_inputs(candidate, "baseline", tmp_path, evidence)
