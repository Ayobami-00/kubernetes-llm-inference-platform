"""Lifecycle activation binds existing custody and cannot silently rotate its seed."""

import json
from copy import deepcopy
from types import SimpleNamespace

import cluster
import lambda_lifecycle_activation as activation
import pytest
from test_lambda_prerequisites import spec as prerequisite_spec

spec = prerequisite_spec


def receipt(spec):
    return {
        "schema_version": "klip-lambda-prerequisite-result.v1",
        "state": "ready",
        "spec": spec.model_dump(mode="json"),
        "setup_id": spec.identity,
        "spec_sha256": activation.digest(spec.model_dump(mode="json")),
        "gpu_capacity_provisioned": False,
        "resources": {
            "ssh": {"state": "ready", "id": "b" * 32},
            "firewall": {"state": "ready", "id": "c" * 32},
        },
    }


@pytest.mark.parametrize("mutation", [None, "spec", "state", "identity", "resources", "paid"])
def test_policy_derives_exact_existing_provider_resources(spec, mutation):
    value = receipt(spec)
    if mutation == "spec":
        value["spec"]["provider_credential_sha256"] = "d" * 64
    elif mutation == "state":
        value["resources"]["firewall"]["state"] = "dispatching"
    elif mutation == "identity":
        value["setup_id"] = "another-setup"
    elif mutation == "resources":
        value["resources"]["foreign"] = {"state": "ready"}
    elif mutation == "paid":
        value["gpu_capacity_provisioned"] = True
    if mutation:
        with pytest.raises(ValueError):
            activation.policy_from_receipt(value)
    else:
        policy = activation.policy_from_receipt(value)
        assert policy.ssh_key_name == spec.names["ssh"]
        assert policy.firewall_ruleset_ids == ("c" * 32,)
        assert policy.max_price_cents_per_hour == 129
        assert policy.max_initial_lease_minutes == 90


@pytest.mark.parametrize("configured", [False, True])
def test_staging_is_idempotent_and_requires_live_runtime_approval(
    tmp_path, monkeypatch, spec, configured
):
    home = tmp_path / "operator"
    management = home / "cluster/management"
    monkeypatch.setattr(activation, "state_home", lambda: home)
    monkeypatch.setattr(activation, "folder", lambda: management)
    policy = activation.policy_from_receipt(receipt(spec))
    metadata = {
        "schema_version": "klip-host-executor-metadata.v1",
        "cluster_id": "klip",
        "kube_system_uid": spec.kube_system_uid,
        "bootstrap_public_key": spec.public_key,
        "gpu_inspection_configured": configured,
        "gpu_runtime_review_sha256": activation.hashlib.sha256(
            activation.REVIEW.read_bytes()
        ).hexdigest(),
    }
    monkeypatch.setattr(activation, "executor_metadata", lambda *a: metadata)
    stored = {}
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert kwargs.get("secret") is True
        if "create" in args:
            assert not stored
            assert (management / "lifecycle/intent.json").is_file()
            stored.update(json.loads(kwargs["data"]))
            stored["metadata"]["uid"] = "synthetic-native-uid"
        return json.dumps(stored)

    monkeypatch.setattr(
        activation,
        "observe_cluster",
        lambda: (
            SimpleNamespace(run=run),
            "synthetic-ops-api",
            {"image": "synthetic-pinned-image"},
        ),
    )
    monkeypatch.setattr(cluster, "kubectl", lambda *a: "synthetic-native-uid" if stored else "")
    path = tmp_path / "provider.json"
    path.write_text(json.dumps(receipt(spec)))
    if not configured:
        with pytest.raises(ValueError, match="approval"):
            activation.stage(path)
        assert not (home / "credentials/lambda-bootstrap-seed").exists()
        assert not calls
        return
    first = activation.stage(path)
    assert activation.stage(path) == first
    assert len([c for c in calls if "create" in c]) == 1
    seed = home / "credentials/lambda-bootstrap-seed"
    assert seed.stat().st_size == 32 and seed.stat().st_mode & 0o077 == 0
    assert "seed" not in first and not first["gpu_capacity_provisioned"]
    assert activation.configuration()["policy"] == policy.model_dump(mode="json")
    original = deepcopy(stored)
    stored["data"]["seed"] = "foreign-content"
    with pytest.raises(ValueError, match="never replace"):
        activation.stage(path)
    stored.clear()
    stored.update(original)
    seed.write_bytes(b"x" * 32)
    with pytest.raises(ValueError):
        activation.stage(path)
