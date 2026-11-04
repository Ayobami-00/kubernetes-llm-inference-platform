"""Database initialization cannot silently replace credentials or recover as empty."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from database import assert_empty_archive, validate_receipt


def test_existing_archive_blocks_fresh_initialization():
    observed = []

    def call(*args):
        observed.append(args)
        return {"Contents": [{"Key": "postgresql/management/retained-history"}]}

    with pytest.raises(ValueError, match="explicit recovery"):
        assert_empty_archive(SimpleNamespace(call=call), "fixture-bucket", "postgresql/management/")
    assert "--no-paginate" in observed[0] and "--max-keys" in observed[0]
    assert_empty_archive(
        SimpleNamespace(call=lambda *args: {}), "fixture-bucket", "postgresql/management/"
    )


def test_review_digest_binds_archive_identity_and_values_while_status_can_advance():
    raw = b"imageName: fixture-pinned-image\n"
    receipt = {
        "mode": "initdb",
        "source_sha256": "source",
        "values_sha256": hashlib.sha256(raw).hexdigest(),
        "archive_prefix": "postgresql/management/",
    }
    approval = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
    receipt["approval_sha256"] = approval
    validate_receipt(receipt, approval, "source", raw)
    receipt["mutation_started_at"] = "2026-11-03T00:00:00Z"
    validate_receipt(receipt, approval, "source", raw)
    changed = copy.deepcopy(receipt)
    changed["archive_prefix"] = "unrelated-empty-prefix/"
    with pytest.raises(ValueError, match="unchanged"):
        validate_receipt(changed, approval, "source", raw)
    with pytest.raises(ValueError, match="source/values changed"):
        validate_receipt(receipt, approval, "source", b"different-image")
