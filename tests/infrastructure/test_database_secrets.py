"""A decrypted credential must retain the exact project and application identity."""

import copy

import pytest
from common import CONFIG
from database_secrets import validate_payload


def test_recovery_bundle_cannot_change_project_or_application_identity():
    credential = {
        "username": "management",
        "namespace": "platform-data",
        "cluster_id": CONFIG["cluster_id"],
        "password": "synthetic-value-" * 4,
    }
    payload = {
        "schema_version": "klip-database-secret.v1",
        "cluster_id": CONFIG["cluster_id"],
        "account_id": CONFIG["account_id"],
        "credential_reference": "credential://klip/management-postgresql",
        "credential": credential,
    }
    assert validate_payload(payload) == credential
    for field in ("cluster_id", "account_id", "credential_reference"):
        changed = copy.deepcopy(payload)
        changed[field] = "foreign"
        with pytest.raises(ValueError, match="another platform"):
            validate_payload(changed)
    for field in ("username", "namespace", "password"):
        changed = copy.deepcopy(payload)
        changed["credential"][field] = "wrong"
        with pytest.raises(ValueError, match="invalid credential"):
            validate_payload(changed)
