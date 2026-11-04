"""Reviewed single-A10 lifecycle policy and frozen runtime credential references."""

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from platform_contracts import WorkerGroupSpec
from platform_contracts.common import ContractModel, Sha256
from pydantic import Field, model_validator

from platform_management.services.bootstrap import callback_origin
from platform_management.services.common import AppError

UUID_PATTERN = r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$"
FIREWALL_ID_PATTERN = r"^(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$"


class LambdaLifecyclePolicy(ContractModel):
    schema_version: Literal["klip-lambda-lifecycle.v1"] = "klip-lambda-lifecycle.v1"
    cluster_id: Literal["klip"] = "klip"
    kube_system_uid: str = Field(pattern=UUID_PATTERN)
    instance_type: Literal["gpu_1x_a10"] = "gpu_1x_a10"
    region: Literal["us-east-1"] = "us-east-1"
    image_id: str = Field(pattern=UUID_PATTERN)
    image_version: str = Field(default="24.4.2-20250626", pattern=r"^24\.[0-9.]+-[0-9]{8}$")
    ssh_key_name: str = Field(pattern=r"^klip-[a-z0-9-]{1,54}$")
    ssh_public_key_sha256: Sha256
    firewall_ruleset_ids: tuple[str, ...] = Field(min_length=1, max_length=4)
    max_price_cents_per_hour: int = Field(default=129, strict=True, ge=1, le=129)
    max_initial_lease_minutes: int = Field(default=90, strict=True, ge=1, le=90)
    max_total_lease_minutes: int = Field(default=180, strict=True, ge=1, le=180)
    host_timeout_s: int = Field(default=1200, strict=True, ge=60, le=1200)
    cleanup_margin_s: int = Field(default=300, strict=True, ge=120, le=600)

    @model_validator(mode="after")
    def bounded_resources(self):
        if (
            self.max_total_lease_minutes < self.max_initial_lease_minutes
            or len(set(self.firewall_ruleset_ids)) != len(self.firewall_ruleset_ids)
            or any(
                not re.fullmatch(FIREWALL_ID_PATTERN, item) for item in self.firewall_ruleset_ids
            )
        ):
            raise ValueError("Invalid lease or provider firewall identities")
        return self

    def check_group(self, spec: WorkerGroupSpec):
        if (
            spec.provider != "lambda"
            or spec.instance_type != self.instance_type
            or spec.region != self.region
            or spec.desired_instances != 1
            or spec.hardware.vendor != "nvidia"
            or spec.hardware.gpu_model != "A10"
            or spec.hardware.gpu_count != 1
            or spec.worker_class != "latency"
            or spec.host_profile != "lambda-gpu-worker-v1"
            or spec.ttl_minutes > self.max_initial_lease_minutes
            or spec.ttl_minutes * 60 <= self.cleanup_margin_s
            or spec.drain_timeout_s > 600
        ):
            raise AppError(
                422,
                "Worker specification exceeds the reviewed single-A10 policy",
                "capacity_policy",
            )

    def check_metadata(self, metadata):
        key = metadata.get("bootstrap_public_key", "")
        if (
            metadata.get("schema_version") != "klip-host-executor-metadata.v1"
            or metadata.get("cluster_id") != self.cluster_id
            or metadata.get("kube_system_uid") != self.kube_system_uid
            or not isinstance(key, str)
            or hashlib.sha256(key.encode()).hexdigest() != self.ssh_public_key_sha256
        ):
            raise AppError(
                409, "Executor cluster or bootstrap identity changed", "host_identity_conflict"
            )


def public_fingerprints(folder):
    root = Path(folder)
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ("ca.crt", "tls.crt")
    }


def configuration(settings):
    if not settings.real_lifecycle_enabled:
        return {}
    try:
        path = settings.lambda_lifecycle_policy_file
        if path is None or path.stat().st_size > 65536:
            raise ValueError("Policy missing or oversized")
        policy = LambdaLifecyclePolicy.model_validate(json.loads(path.read_text()))
        tls_home, seed_path = settings.host_executor_tls_home, settings.bootstrap_seed_file
        if (
            tls_home is None
            or seed_path is None
            or not tls_home.is_absolute()
            or not seed_path.is_absolute()
        ):
            raise ValueError("Runtime references must be explicit mount paths")
        seed = seed_path.read_bytes()
        if len(seed) != 32 or not (tls_home / "tls.key").is_file():
            raise ValueError("Runtime credential material is absent")
        provider_key = settings.lambda_api_key_file.read_text().strip()
        if not provider_key or any(c in provider_key for c in "\r\n"):
            raise ValueError("Provider credential material is absent")
        origin = callback_origin(settings.allowed_origin)
        return {
            "provider": "lambda:v1",
            "host": "isolated-host:v1",
            "cluster": "isolated-host:v1",
            "lifecycle": {
                "policy": policy.model_dump(mode="json"),
                "origin": origin,
                # Kubernetes projected Secret targets are Pod-specific timestamps.
                # Freeze the stable mount reference, never its current symlink target.
                "lambda_api_key_file": str(settings.lambda_api_key_file.absolute()),
                "lambda_api_key_sha256": hashlib.sha256(provider_key.encode()).hexdigest(),
                "bootstrap_seed_file": str(seed_path),
                "bootstrap_seed_sha256": hashlib.sha256(seed).hexdigest(),
                "host_executor_tls_home": str(tls_home),
                "tls_fingerprints": public_fingerprints(tls_home),
            },
        }
    except (OSError, ValueError, TypeError):
        raise AppError(
            422,
            "Real Lambda lifecycle configuration is incomplete or invalid",
            "adapter_unavailable",
        ) from None


def checked_binding(binding):
    if (
        binding.get("mode") != "real"
        or binding.get("provider") != "lambda:v1"
        or binding.get("host") != "isolated-host:v1"
        or binding.get("cluster") != "isolated-host:v1"
        or not binding.get("lifecycle")
    ):
        raise AppError(
            422, "Real worker lifecycle adapters are unconfigured", "adapter_unavailable"
        )
    return LambdaLifecyclePolicy.model_validate(binding["lifecycle"]["policy"])
