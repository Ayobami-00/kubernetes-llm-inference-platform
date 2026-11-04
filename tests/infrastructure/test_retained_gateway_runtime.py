"""An accepted desired policy is insufficient without effective per-replica evidence."""

import json

import cluster
import pytest
import retained_gateway_runtime

DIGEST = "sha256:00c38048aeb4bb4b49d9ec6f3d7742a7c9f3d4d33f0371632a76734c02bebb7d"


@pytest.fixture
def gateway(monkeypatch):
    pods = [
        {
            "metadata": {"name": "inference-" + letter, "uid": "synthetic-" + letter},
            "spec": {"nodeName": "synthetic-cpu-" + letter},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [{"name": "envoy", "imageID": "synthetic@" + DIGEST}],
            },
        }
        for letter in ("a", "b")
    ]
    results = {p["metadata"]["name"]: [["3s"]] for p in pods}
    inspected = []

    class Host:
        def __init__(self, *args):
            pass

        def run(self, arguments, *, data, timeout):
            assert arguments[:3] == ["sudo", "python3", "-c"]
            pod = json.loads(data)["pod"]
            inspected.append(pod)
            return json.dumps(
                {"pod": pod, "matched_clusters": 1, "http_idle_timeout": results[pod]}
            )

    monkeypatch.setattr(cluster, "Host", Host)
    monkeypatch.setattr(cluster, "load_hosts", lambda: {"cp-01": {}})
    monkeypatch.setattr(cluster, "kubectl", lambda *args: json.dumps({"items": pods}))
    return pods, results, inspected


def test_runtime_proof_covers_both_separate_ready_replicas(gateway):
    _, _, inspected = gateway
    result = retained_gateway_runtime.observe()
    assert len(result["replicas"]) == 2
    assert set(inspected) == {"inference-a", "inference-b"}
    assert all(row["http_idle_timeout"] == "3s" for row in result["replicas"])


@pytest.mark.parametrize("fault", ["missing-timeout", "wrong-timeout", "same-host", "not-ready"])
def test_runtime_gate_rejects_configuration_or_replica_mismatch(gateway, fault):
    pods, results, _ = gateway
    if fault == "missing-timeout":
        results["inference-b"] = [[]]
    elif fault == "wrong-timeout":
        results["inference-b"] = [["3600s"]]
    elif fault == "same-host":
        pods[1]["spec"]["nodeName"] = pods[0]["spec"]["nodeName"]
    else:
        pods[1]["status"]["conditions"][0]["status"] = "False"
    with pytest.raises(ValueError):
        retained_gateway_runtime.observe()
