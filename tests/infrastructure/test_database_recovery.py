"""Recovery cannot silently use another backup, database identity or injected SQL."""

import copy
import hashlib
import json

import pytest
from database_recovery import (
    assert_backup_owned,
    marker_statement,
    operation_paths,
    validate_restore_plan,
)


def test_backup_identity_is_bound_to_intent_and_uid():
    receipt = {
        "operation": "012345abcdef",
        "backup_name": "klip-recovery-012345abcdef",
        "backup_uid": "owned-uid",
    }
    resource = {
        "metadata": {
            "name": receipt["backup_name"],
            "uid": "owned-uid",
            "labels": {"klip.dev/recovery-operation": receipt["operation"]},
        },
        "spec": {
            "cluster": {"name": "klip-db"},
            "method": "plugin",
            "pluginConfiguration": {"name": "barman-cloud.cloudnative-pg.io"},
        },
    }
    assert_backup_owned(resource, receipt)
    for section, field, value in (
        ("metadata", "uid", "replaced"),
        ("spec", "method", "volumeSnapshot"),
    ):
        changed = copy.deepcopy(resource)
        changed[section][field] = value
        with pytest.raises(ValueError, match="identity or ownership"):
            assert_backup_owned(changed, receipt)


@pytest.mark.parametrize(
    "operation,cohort,payload",
    [
        ("x';DROP TABLE", "base", "a" * 64),
        ("012345abcdef", "injected'", "a" * 64),
        ("012345abcdef", "base", "'" * 64),
    ],
)
def test_marker_values_cannot_escape_sql(operation, cohort, payload):
    with pytest.raises(ValueError, match="Invalid recovery marker"):
        marker_statement(operation, cohort, payload)


def test_operation_cannot_escape_private_state():
    with pytest.raises(ValueError, match="stable 12-character"):
        operation_paths("../../credentials")


def test_restore_review_binds_source_backup_and_values_but_not_progress():
    values = b"bootstrap:\n  mode: recovery\n"
    plan = {
        "cluster": "klip-db-restore",
        "source_cluster_uid": "original",
        "source_sha256": "source",
        "backup_id": "20261103T171050",
        "values_sha256": hashlib.sha256(values).hexdigest(),
    }
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    plan["approval_sha256"] = digest
    validate_restore_plan(plan, digest, values, "source")
    plan["mutation_started_at"] = "2026-11-03T17:00:00Z"
    plan["cluster_uid"] = "actual-restore"
    validate_restore_plan(plan, digest, values, "source")
    changed = copy.deepcopy(plan)
    changed["source_cluster_uid"] = "another-database"
    with pytest.raises(ValueError, match="changed after review"):
        validate_restore_plan(changed, digest, values, "source")
    with pytest.raises(ValueError, match="changed after review"):
        validate_restore_plan(plan, digest, b"bootstrap: {mode: initdb}", "source")
