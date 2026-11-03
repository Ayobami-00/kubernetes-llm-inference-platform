"""Leaf renewal must preserve identity and never repeat an ambiguous submission."""

import copy
import json
from unittest.mock import Mock

import cluster
import management_renewal as renewal
import pytest


def cert():
    return {
        "metadata": {
            "uid": "certificate-one",
            "name": "klip-web",
            "resourceVersion": "12",
            "generation": 1,
        },
        "spec": {"secretName": "klip-web-tls"},
        "status": {
            "revision": 1,
            "conditions": [
                {"type": "Ready", "status": "True"},
                {"type": "Issuing", "status": "False"},
            ],
        },
    }


def test_manual_issuance_keeps_ready_condition_and_uses_api_preconditions():
    value = cert()
    before = copy.deepcopy(value)
    patch = renewal.renewal_patch(value)
    assert patch[:2] == [
        {"op": "test", "path": "/metadata/uid", "value": "certificate-one"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "12"},
    ]
    conditions = patch[-1]["value"]
    assert conditions[0] == {"type": "Ready", "status": "True"}
    assert conditions[-1]["reason"] == "ManuallyTriggered"
    assert conditions[-1]["status"] == "True"
    assert value == before
    value["status"]["conditions"][-1]["status"] = "True"
    with pytest.raises(ValueError, match="already active"):
        renewal.renewal_patch(value)
    value = cert()
    value["spec"]["secretName"] = "another-secret"
    with pytest.raises(ValueError, match="identity"):
        renewal.renewal_patch(value)


def test_ambiguous_submission_is_not_repeated_and_terminal_proof_is_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(renewal, "folder", lambda: tmp_path)
    operation = "123456abcdef"
    root = tmp_path / "renewal" / operation
    root.mkdir(parents=True)
    path = root / "verification.json"
    record = {"stage": "triggering", "certificate_uid": "certificate-one", "revision_before": 1}
    path.write_text(json.dumps(record))
    monkeypatch.setattr(renewal, "certificate", cert)
    mutate = Mock(side_effect=AssertionError("a second issuance is forbidden"))
    monkeypatch.setattr(cluster, "kubectl", mutate)
    with pytest.raises(ValueError, match="Ambiguous"):
        renewal.prove(operation)
    mutate.assert_not_called()
    record["stage"] = "passed"
    path.write_text(json.dumps(record))
    assert renewal.prove(operation) == record
    mutate.assert_not_called()
