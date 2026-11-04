"""The availability fix must not change database identity, storage or durability."""

import copy

import pytest
import yaml
from database_tuning import assert_only_shutdown_change


@pytest.mark.parametrize("extra", [None, "storage", "image", "quorum", "bootstrap", "resource"])
def test_tuning_changes_only_the_smart_shutdown_window(extra):
    before = [
        {
            "apiVersion": "postgresql.cnpg.io/v1",
            "kind": "Cluster",
            "metadata": {"name": "klip-db"},
            "spec": {
                "storage": {"size": "40Gi"},
                "imageName": "pinned-image",
                "postgresql": {"synchronous": {"dataDurability": "required"}},
                "bootstrap": {"initdb": {"database": "management"}},
            },
        }
    ]
    after = copy.deepcopy(before)
    after[0]["spec"]["smartShutdownTimeout"] = 5
    if extra == "storage":
        after[0]["spec"]["storage"]["size"] = "20Gi"
    if extra == "image":
        after[0]["spec"]["imageName"] = "different-image"
    if extra == "quorum":
        after[0]["spec"]["postgresql"]["synchronous"]["dataDurability"] = "preferred"
    if extra == "bootstrap":
        after[0]["spec"]["bootstrap"] = {"recovery": {}}
    if extra == "resource":
        after.append({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "unexpected"}})
    old, new = yaml.safe_dump_all(before), yaml.safe_dump_all(after)
    if extra:
        with pytest.raises(ValueError):
            assert_only_shutdown_change(old, new)
    else:
        assert assert_only_shutdown_change(old, new) == [
            {"resource": "klip-db", "field": "spec.smartShutdownTimeout", "value": 5}
        ]
