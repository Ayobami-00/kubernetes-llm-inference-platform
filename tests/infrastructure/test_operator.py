import hashlib
import json

import pytest
from common import CONFIG, Aws, write_private
from manage import teardown_evidence, verify_receipt, verify_teardown_evidence


@pytest.fixture
def receipt(tmp_path):
    plan, values = tmp_path / "saved.tfplan", tmp_path / "variables.json"
    plan.write_bytes(b"reviewed-plan")
    values.write_text("{}")
    return {
        "root": "foundation",
        "preview": False,
        "account_id": CONFIG["account_id"],
        "source_sha256": "reviewed-source",
        "plan_path": str(plan),
        "variables_path": str(values),
        "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "variables_sha256": hashlib.sha256(values.read_bytes()).hexdigest(),
    }


def test_exact_reviewed_plan_is_required(receipt):
    verify_receipt(receipt, receipt["plan_sha256"], "reviewed-source", "foundation")
    with pytest.raises(ValueError, match="exact saved plan"):
        verify_receipt(receipt, "another-plan", "reviewed-source", "foundation")


def test_preview_plans_can_never_be_applied(receipt):
    receipt["preview"] = True
    with pytest.raises(ValueError, match="Preview"):
        verify_receipt(receipt, receipt["plan_sha256"], "reviewed-source", "foundation")


def test_changed_sources_inputs_or_root_require_new_review(receipt):
    with pytest.raises(ValueError, match="implementation changed"):
        verify_receipt(receipt, receipt["plan_sha256"], "new-source", "foundation")
    with pytest.raises(ValueError, match="root differs"):
        verify_receipt(receipt, receipt["plan_sha256"], "reviewed-source", "bootstrap")
    with open(receipt["variables_path"], "w") as stream:
        json.dump({"changed": True}, stream)
    with pytest.raises(ValueError, match="inputs changed"):
        verify_receipt(receipt, receipt["plan_sha256"], "reviewed-source", "foundation")


def test_already_applied_receipt_is_not_replayed(receipt):
    receipt["applied_at"] = "2026-11-03T00:00:00Z"
    with pytest.raises(ValueError, match="already applied"):
        verify_receipt(receipt, receipt["plan_sha256"], "reviewed-source", "foundation")


def test_completed_apply_is_not_replayed_after_migration_failure(receipt):
    receipt["terraform_applied_at"] = "2026-11-03T00:00:00Z"
    with pytest.raises(ValueError, match="already applied"):
        verify_receipt(receipt, receipt["plan_sha256"], "reviewed-source", "foundation")


def test_wrong_cloud_account_is_rejected_before_work(monkeypatch):
    monkeypatch.setattr(
        Aws, "call", lambda *args, **kwargs: {"Account": "999999999999", "Arn": "wrong"}
    )
    with pytest.raises(ValueError, match="Wrong AWS account"):
        Aws("unused-offline-profile").preflight("foundation")


def test_admin_identity_cannot_silently_replace_deployment_role(monkeypatch):
    monkeypatch.setattr(
        Aws,
        "call",
        lambda *args, **kwargs: {
            "Account": CONFIG["account_id"],
            "Arn": f"arn:aws:iam::{CONFIG['account_id']}:user/{CONFIG['source_user']}",
        },
    )
    with pytest.raises(ValueError, match="Unexpected foundation identity"):
        Aws("unused-offline-profile").preflight("foundation")


def test_teardown_requires_explicit_cluster_and_data_decision():
    with pytest.raises(ValueError, match="confirm-cluster"):
        teardown_evidence(None, True, None, None, False)
    with pytest.raises(ValueError, match="backup-file"):
        teardown_evidence("klip", False, None, None, False)
    assert teardown_evidence("klip", True, None, None, False)["empty_etcd"]


def test_backup_is_checksummed_and_rechecked_before_destroy(tmp_path):
    path = tmp_path / "etcd-snapshot.db"
    path.write_bytes(b"offline-test-artifact-not-a-real-snapshot")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    evidence = teardown_evidence("klip", False, path, digest, True)
    verify_teardown_evidence(evidence)
    path.write_bytes(b"changed-backup")
    with pytest.raises(ValueError, match="no longer available"):
        verify_teardown_evidence(evidence)


def test_operator_artifacts_are_private(tmp_path):
    path = tmp_path / "private/receipt.json"
    write_private(path, {"account_id": CONFIG["account_id"]})
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
