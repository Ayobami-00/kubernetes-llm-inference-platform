"""Monitoring ownership and immutable plans remain fail-closed across retries."""

import copy
import json
from contextlib import nullcontext
from unittest.mock import Mock

import cluster
import monitoring_runtime as runtime
import platform_resize
import pytest
import yaml


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


def test_archive_preserves_terminal_values_and_detects_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "folder", lambda: tmp_path)
    approval = "synthetic-approval"
    values = {"synthetic": "original"}
    plan = {"approval_sha256": approval, "values_sha256": runtime.sha(values)}
    (tmp_path / (approval + ".json")).write_text(json.dumps(plan))
    (tmp_path / "active.json").write_text(
        json.dumps({"approval_sha256": approval, "status": "installed"})
    )
    (tmp_path / "values.yaml").write_text(yaml.safe_dump(values))
    runtime.archive_current()
    path = tmp_path / "history" / approval / "values.yaml"
    assert yaml.safe_load(path.read_text()) == values
    (tmp_path / "values.yaml").write_text("synthetic: new-release\n")
    runtime.archive_current()
    assert yaml.safe_load(path.read_text()) == values
    path.write_text("synthetic: changed-history\n")
    with pytest.raises(ValueError, match="Archived monitoring receipt changed"):
        runtime.archive_current()


def test_operator_admission_needs_fail_policy_ca_and_exact_service(monkeypatch):
    document = {
        "metadata": {"annotations": {"meta.helm.sh/release-name": runtime.RELEASE}},
        "webhooks": [
            {
                "failurePolicy": "Fail",
                "clientConfig": {
                    "caBundle": "synthetic-public-ca",
                    "service": {
                        "name": "klip-monitoring-operator",
                        "namespace": runtime.NAMESPACE,
                        "port": 443,
                    },
                },
            }
        ],
    }
    monkeypatch.setattr(runtime, "get", lambda *_: document)
    assert runtime.admission_fail_closed()
    document["webhooks"][0]["failurePolicy"] = "Ignore"
    assert not runtime.admission_fail_closed()
    document["webhooks"][0]["failurePolicy"] = "Fail"
    document["webhooks"][0]["clientConfig"]["caBundle"] = ""
    assert not runtime.admission_fail_closed()


def test_only_proven_pre_helm_failures_can_be_retired(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "folder", lambda: tmp_path)
    monkeypatch.setattr(runtime, "archive_current", Mock())
    monkeypatch.setattr(runtime, "observe", lambda: {"ready": True})
    monkeypatch.setattr(cluster, "kubectl", Mock(return_value=""))
    current = {"approval_sha256": "original", "status": "interrupted"}
    path = tmp_path / "active.json"
    path.write_text(json.dumps(current))
    result = runtime.reconcile("original")
    assert result["status"] == "failed" and result["failed_before_helm"]
    current["interrupted_stage"] = "installing"
    path.write_text(json.dumps(current))
    with pytest.raises(ValueError, match="ambiguous Helm"):
        runtime.reconcile("original")


def test_postcondition_failure_needs_observed_deployed_values(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "folder", lambda: tmp_path)
    monkeypatch.setattr(runtime, "archive_current", Mock())
    monkeypatch.setattr(runtime, "observe", lambda: {"ready": True})
    monkeypatch.setattr(cluster, "kubectl", Mock(return_value=""))
    monkeypatch.setattr(cluster, "kubeconfigs", Mock())
    monkeypatch.setattr(cluster, "tunnel", nullcontext)
    values = {"synthetic": "attempted-values"}
    current = {
        "approval_sha256": "original",
        "status": "interrupted",
        "interrupted_stage": "hardening-admission",
        "error_type": "ValueError",
    }
    path = tmp_path / "active.json"
    path.write_text(json.dumps(current))
    (tmp_path / "original.json").write_text(json.dumps({"values_sha256": runtime.sha(values)}))
    (tmp_path / "helm-admission.log").write_text("synthetic completed Helm command")
    status = {"info": {"status": "deployed"}, "version": 3, "config": values}
    monkeypatch.setattr(cluster, "checked", lambda *_args, **_kwargs: json.dumps(status))
    observed = runtime.reconcile("original")
    assert observed["failed_after_helm"] and not observed["failed_before_helm"]
    assert observed["observed_helm_revision"] == 3
    path.write_text(json.dumps(current))
    status["config"] = {"synthetic": "different-values"}
    with pytest.raises(ValueError, match="differs from"):
        runtime.reconcile("original")
    current["interrupted_stage"] = "installing-crds"
    path.write_text(json.dumps(current))
    monkeypatch.setattr(cluster, "kubectl", Mock(return_value="secret/pending-upgrade"))
    with pytest.raises(ValueError, match="not stable"):
        runtime.reconcile("original")
