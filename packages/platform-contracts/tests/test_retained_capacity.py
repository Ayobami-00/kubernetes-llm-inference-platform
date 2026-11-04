import copy
import json

import pytest
from platform_contracts import WorkerGroup, WorkerGroupSpec


def example(root):
    return json.loads((root / "contracts/fixtures/worker-groups/ready.json").read_text())


def test_legacy_leased_capacity_keeps_its_serialized_shape(root):
    data = example(root)
    assert WorkerGroup.model_validate(data).model_dump(mode="json") == data


def test_retained_capacity_records_allocation_without_a_lease(root):
    data = example(root)
    data["spec"].update(capacity_lifecycle="externally_retained", ttl_minutes=None)
    data["status"].update(capacity_lifecycle="externally_retained", expires_at=None)
    group = WorkerGroup.model_validate(data)
    assert group.status.allocated_at is not None and group.status.expires_at is None
    assert group.spec.ttl_minutes is None
    assert WorkerGroup.model_validate_json(group.model_dump_json()) == group
    for mutate in [
        lambda d: d["status"].update(capacity_lifecycle="leased"),
        lambda d: d["status"].update(phase="expired"),
        lambda d: d["status"].update(expires_at=example(root)["status"]["expires_at"]),
    ]:
        bad = copy.deepcopy(data)
        mutate(bad)
        with pytest.raises(ValueError):
            WorkerGroup.model_validate(bad)


@pytest.mark.parametrize("retained,ttl", [(True, 90), (False, None)])
def test_lifecycle_and_ttl_cannot_conflict(root, retained, ttl):
    data = example(root)["spec"]
    data.update(capacity_lifecycle="externally_retained" if retained else "leased", ttl_minutes=ttl)
    with pytest.raises(ValueError):
        WorkerGroupSpec.model_validate(data)
