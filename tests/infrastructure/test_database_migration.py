"""Migration preserves existing target data and binds the source dump to review."""

import hashlib
import json

import pytest
from database_migration import fingerprint_query, validate_receipt


def test_review_rejects_a_changed_dump_or_database_target():
    dump = b"synthetic dump content"
    receipt = {
        "target_cluster_uid": "owned-database",
        "dump_sha256": hashlib.sha256(dump).hexdigest(),
    }
    digest = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
    receipt["approval_sha256"] = digest
    validate_receipt(receipt, digest, dump)
    receipt["mutation_started_at"] = "2026-11-03T17:00:00Z"
    validate_receipt(receipt, digest, dump)
    with pytest.raises(ValueError, match="changed after review"):
        validate_receipt(receipt, digest, b"different source data")
    receipt["target_cluster_uid"] = "another-database"
    with pytest.raises(ValueError, match="changed after review"):
        validate_receipt(receipt, digest, dump)


def test_table_names_cannot_inject_sql_into_reconciliation():
    with pytest.raises(ValueError, match="table identifier"):
        fingerprint_query('runs"; DROP SCHEMA public CASCADE; --')


def test_restore_refuses_existing_target_data_before_any_mutation(tmp_path, monkeypatch):
    import database_migration as migration

    dump = tmp_path / "source.dump"
    dump.write_bytes(b"synthetic dump")
    path = tmp_path / "receipt.json"
    receipt = {
        "target_cluster_uid": "owned",
        "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
    }
    digest = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
    receipt["approval_sha256"] = digest
    path.write_text(json.dumps(receipt))
    monkeypatch.setattr(migration, "paths", lambda: (tmp_path, path, dump))
    monkeypatch.setattr(migration, "resource", lambda *args: {"metadata": {"uid": "owned"}})
    monkeypatch.setattr(migration, "sql", lambda *args: '["existing_customer_data"]')
    with pytest.raises(ValueError, match="refusing to overwrite"):
        migration.restore(digest)
    assert "mutation_started_at" not in json.loads(path.read_text())
