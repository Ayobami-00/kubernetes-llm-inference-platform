import json

import pytest
from platform_contracts import WorkerGroup


def test_manual_deletion_does_not_require_expiry(root):
    data = json.loads((root / "contracts/fixtures/worker-groups/deleted.json").read_text())
    group = WorkerGroup.model_validate(data)
    assert group.status.observed_at < group.status.expires_at
    data["status"]["members"][0]["phase"] = "allocated"
    data["status"]["counts"]["allocated_instances"] = 1
    with pytest.raises(ValueError):
        WorkerGroup.model_validate(data)
