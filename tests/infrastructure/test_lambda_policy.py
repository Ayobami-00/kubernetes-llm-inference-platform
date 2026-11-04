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
    assert configuration(Settings(real_lifecycle_enabled=False)) == {}
    with pytest.raises(AppError, match="incomplete or invalid"):
        configuration(Settings(real_lifecycle_enabled=True))


@pytest.mark.parametrize("projected", [False, True])
def test_configuration_freezes_pins_and_secret_references_only(tmp_path, monkeypatch, projected):
    path, seed, tls = tmp_path / "policy.json", tmp_path / "bootstrap", tmp_path / "tls"
    path.write_text(policy().model_dump_json())
    seed.write_bytes(b"s" * 32)
    provider = tmp_path / "lambda-key"
    key = "synthetic-provider-key-material"
    if projected:
        generation = tmp_path / "..2026_11_04_09_32_28.2670861417"
        generation.mkdir()
        (generation / "lambda-key").write_text(key)
        (tmp_path / "..data").symlink_to(generation.name)
        provider.symlink_to("..data/lambda-key")
    else:
        provider.write_text(key)
    tls.mkdir()
    for name in ("ca.crt", "tls.crt", "tls.key"):
        (tls / name).write_text("synthetic-" + name + "-material")
    settings = Settings(
        real_lifecycle_enabled=True,
        lambda_lifecycle_policy_file=path,
        bootstrap_seed_file=seed,
        host_executor_tls_home=tls,
        lambda_api_key_file=provider,
        allowed_origin="https://management.example.test",
    )
    result = configuration(settings)
    assert result["provider"] == "lambda:v1" and result["host"] == "isolated-host:v1"
    serialized = json.dumps(result)
    assert "s" * 32 not in serialized and "synthetic-tls.key-material" not in serialized
    assert "synthetic-provider-key-material" not in serialized
    assert result["lifecycle"]["bootstrap_seed_file"] == str(seed)
    assert result["lifecycle"]["lambda_api_key_file"] == str(provider)
    assert set(result["lifecycle"]["tls_fingerprints"]) == {"ca.crt", "tls.crt"}
    if projected:
        # Another Pod, or a kubelet rotation, has a different projection target.
        replacement = tmp_path / "..2026_11_04_09_35_00.1234567890"
        replacement.mkdir()
        (replacement / "lambda-key").write_text(key)
        (tmp_path / "..data").unlink()
        (tmp_path / "..data").symlink_to(replacement.name)
        (generation / "lambda-key").unlink()
        generation.rmdir()
        from types import SimpleNamespace

        from platform_management.jobs import real_worker

        calls = []

        def provider_client(value, **kwargs):
            calls.append(value)
            return SimpleNamespace(close=lambda: None)

        monkeypatch.setattr(real_worker, "LambdaMutations", provider_client)
        with real_worker.RuntimeIO(result).provider():
            pass
        assert calls == [key]
        (replacement / "lambda-key").write_text("different-synthetic-account")
        with pytest.raises(AppError, match="original account identity"):
            with real_worker.RuntimeIO(result).provider():
                pytest.fail("Changed keys must not reach the provider")
        assert calls == [key]


def test_missing_runtime_key_omits_mount_paths(tmp_path):
    from platform_management.jobs.real_worker import RuntimeIO

    with pytest.raises(AppError) as captured:
        with RuntimeIO(
            {"lifecycle": {"lambda_api_key_file": str(tmp_path / "private-mount")}}
        ).provider():
            pytest.fail("Missing credential must fail before transport construction")
    assert str(tmp_path) not in captured.value.message


def test_changed_provider_credential_cannot_be_used_to_confirm_another_accounts_absence(
    tmp_path, monkeypatch
):
    from platform_management.jobs import real_worker

    path = tmp_path / "lambda-key"
    path.write_text("different-synthetic-account-key")
    constructor = []
    monkeypatch.setattr(
        real_worker, "LambdaMutations", lambda *args, **kwargs: constructor.append(True)
    )
    io = real_worker.RuntimeIO(
        {
            "lifecycle": {
                "lambda_api_key_file": str(path),
                "lambda_api_key_sha256": hashlib.sha256(
                    b"original-synthetic-account-key"
                ).hexdigest(),
            }
        }
    )
    with pytest.raises(AppError, match="original account identity"):
        with io.provider():
            pytest.fail("No provider request may be made after the identity changed")
    assert not constructor
