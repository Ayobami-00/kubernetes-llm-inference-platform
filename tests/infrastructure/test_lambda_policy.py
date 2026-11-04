"""Reviewed lifecycle bindings are public pins/references, never credential values."""

import hashlib
import json

import pytest
from platform_contracts import WorkerGroupSpec
from platform_management.integrations.lambda_policy import LambdaLifecyclePolicy, configuration
from platform_management.services.common import AppError
from platform_management.settings import Settings


def policy(**changes):
    return LambdaLifecyclePolicy.model_validate(
        {
            "kube_system_uid": "12345678-1234-1234-1234-123456789abc",
            "image_id": "87654321-4321-4321-4321-cba987654321",
            "ssh_key_name": "klip-test-key",
            "ssh_public_key_sha256": hashlib.sha256(b"synthetic-public-key").hexdigest(),
            "firewall_ruleset_ids": ["11111111-2222-3333-4444-555555555555"],
            **changes,
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"instance_type": "gpu_8x_a100"},
        {"region": "us-west-1"},
        {"max_initial_lease_minutes": 91},
        {"max_total_lease_minutes": 181},
        {"max_price_cents_per_hour": 130},
        {"host_timeout_s": 1201},
        {"firewall_ruleset_ids": ["unknown"]},
        {"arbitrary_command": "untrusted"},
        {"firewall_ruleset_ids": ["11111111-2222-3333-4444-555555555555"] * 2},
    ],
)
def test_resource_envelope_is_explicit_and_bounded(changes):
    with pytest.raises(ValueError):
        policy(**changes)


def test_draft_or_incompatible_group_cannot_use_real_policy():
    document = WorkerGroupSpec(
        id="test-workers",
        revision=1,
        instance_type="gpu_1x_a10",
        region="us-east-1",
        hardware={"vendor": "nvidia", "gpu_model": "A10", "gpu_count": 1, "vram_gib_per_gpu": 24},
        worker_class="latency",
    )
    policy().check_group(document)
    for change in (
        {"instance_type": None},
        {"region": None},
        {"desired_instances": 2},
        {"ttl_minutes": 120},
    ):
        with pytest.raises(AppError):
            policy().check_group(document.model_copy(update=change))


def test_executor_metadata_must_match_pinned_cluster_and_public_key():
    document = policy()
    metadata = {
        "schema_version": "klip-host-executor-metadata.v1",
        "cluster_id": "klip",
        "kube_system_uid": document.kube_system_uid,
        "bootstrap_public_key": "synthetic-public-key",
    }
    document.check_metadata(metadata)
    with pytest.raises(AppError, match="identity changed"):
        document.check_metadata({**metadata, "bootstrap_public_key": "different-public-key"})


def test_disabled_and_incomplete_configuration_fail_closed(tmp_path):
    assert configuration(Settings()) == {}
    with pytest.raises(AppError, match="incomplete or invalid"):
        configuration(Settings(real_lifecycle_enabled=True))


def test_configuration_freezes_pins_and_secret_references_only(tmp_path):
    path, seed, tls = tmp_path / "policy.json", tmp_path / "bootstrap", tmp_path / "tls"
    path.write_text(policy().model_dump_json())
    seed.write_bytes(b"s" * 32)
    tls.mkdir()
    for name in ("ca.crt", "tls.crt", "tls.key"):
        (tls / name).write_text("synthetic-" + name + "-material")
    settings = Settings(
        real_lifecycle_enabled=True,
        lambda_lifecycle_policy_file=path,
        bootstrap_seed_file=seed,
        host_executor_tls_home=tls,
        allowed_origin="https://management.example.test",
    )
    result = configuration(settings)
    assert result["provider"] == "lambda:v1" and result["host"] == "isolated-host:v1"
    serialized = json.dumps(result)
    assert "s" * 32 not in serialized and "synthetic-tls.key-material" not in serialized
    assert result["lifecycle"]["bootstrap_seed_file"] == str(seed)
    assert set(result["lifecycle"]["tls_fingerprints"]) == {"ca.crt", "tls.crt"}
