"""A rolling application update cannot erase its prior verified release definition."""

import hashlib
import json

import pytest
from management_release import retain_active_release


def test_previous_verified_release_is_retained_exactly_and_repeated_reads_are_safe(tmp_path):
    values, rendered = b"image: pinned\n", b"kind: Deployment\n"
    plan = {
        "approval_sha256": "a" * 64,
        "values_sha256": hashlib.sha256(values).hexdigest(),
        "manifest_sha256": hashlib.sha256(rendered).hexdigest(),
    }
    files = {
        "active.json": json.dumps({"approval_sha256": "a" * 64}).encode(),
        "plan.json": json.dumps(plan).encode(),
        "values.yaml": values,
        "rendered.yaml": rendered,
    }
    for name, body in files.items():
        (tmp_path / name).write_bytes(body)
    retain_active_release(tmp_path)
    retain_active_release(tmp_path)
    for name, body in files.items():
        assert (tmp_path / "release-history" / ("a" * 64) / name).read_bytes() == body
        assert (tmp_path / name).read_bytes() == body
    (tmp_path / "values.yaml").write_bytes(b"unreviewed change")
    with pytest.raises(ValueError, match="Reconcile"):
        retain_active_release(tmp_path)
    assert (tmp_path / "release-history" / ("a" * 64) / "values.yaml").read_bytes() == values
