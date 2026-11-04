"""Typed requests to the isolated host executor; never commands or credential paths."""

import base64
from ipaddress import IPv4Address, IPv4Network
from typing import Literal, Self

from pydantic import Field, model_serializer, model_validator

from .common import ContractModel, ErrorInfo, NonEmpty, PositiveInt, Sha256, Timestamp
from .inventory import GPUCapacityObservation


def ed25519_host_key(value: str) -> str:
    fields = value.split() if isinstance(value, str) else []
    try:
        blob = base64.b64decode(fields[1], validate=True) if len(fields) == 2 else b""
    except ValueError:
        blob = b""
    if (
        not isinstance(value, str)
        or "\n" in value
        or "\r" in value
        or len(fields) != 2
        or fields[0] != "ssh-ed25519"
        or len(blob) != 51
        or blob[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
    ):
        raise ValueError("Expected one Ed25519 public host key")
    return "ssh-ed25519 " + base64.b64encode(blob).decode()


class ProviderFence(ContractModel):
    """Management's authenticated receipt for an owned, confirmed terminated VM."""

    intent_id: str = Field(pattern=r"^op-[a-f0-9]{16,32}$")
    provider_instance_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    termination_requested_at: Timestamp
    confirmed_at: Timestamp
    observation_sha256: Sha256

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.confirmed_at < self.termination_requested_at:
            raise ValueError("Provider fence confirmation predates its termination request")
        return self


class HostTask(ContractModel):
    schema_version: Literal["0.1"] = "0.1"
    id: str = Field(pattern=r"^host-[a-f0-9]{24}$")
    cluster_id: Literal["klip"] = "klip"
    action: Literal["prepare", "join", "inspect", "remove", "cleanup-check", "revoke-bootstrap"]
    provider_instance_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    node_name: str = Field(pattern=r"^gpu-[a-f0-9]{12,32}$")
    node_uid: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$"
    )
    public_ip: IPv4Address
    private_ip: IPv4Address
    wireguard_ip: IPv4Address
    trusted_host_key: str = Field(max_length=256)
    registry_revision: PositiveInt
    deadline: Timestamp
    previous_task_id: str | None = Field(default=None, pattern=r"^host-[a-f0-9]{24}$")
    drain_timeout_s: int = Field(default=120, strict=True, ge=1, le=600)
    provider_fence: ProviderFence | None = None

    @model_validator(mode="after")
    def bounded_identity(self) -> Self:
        if not self.public_ip.is_global or self.public_ip.is_multicast:
            raise ValueError("GPU executor requires a public provider IPv4 endpoint")
        if any(
            self.private_ip in network
            for network in (
                IPv4Network("127.0.0.0/8"),
                IPv4Network("169.254.0.0/16"),
                IPv4Network("224.0.0.0/4"),
                IPv4Network("0.0.0.0/8"),
                IPv4Network("10.40.0.0/16"),
                IPv4Network("10.200.0.0/24"),
                IPv4Network("10.244.0.0/16"),
                IPv4Network("10.96.0.0/12"),
            )
        ):
            raise ValueError("Invalid provider private IPv4 address")
        if (
            self.wireguard_ip not in IPv4Network("10.200.0.0/24")
            or int(str(self.wireguard_ip).split(".")[-1]) < 67
            or str(self.wireguard_ip).endswith(".255")
        ):
            raise ValueError("GPU addresses must exclude fixed CPU slots and network boundaries")
        self.trusted_host_key = ed25519_host_key(self.trusted_host_key)
        if self.action == "prepare":
            if self.previous_task_id is not None or self.node_uid is not None:
                raise ValueError("Initial preparation cannot claim a prior task or node UID")
        elif self.previous_task_id is None:
            raise ValueError("Follow-up host work requires the original task chain")
        if self.action in {"inspect", "remove"} and self.node_uid is None:
            raise ValueError("Inspection/removal requires the observed node UID")
        if self.previous_task_id == self.id:
            raise ValueError("Host task cannot refer to itself")
        if self.action == "revoke-bootstrap":
            if (
                self.provider_fence is None
                or self.provider_fence.provider_instance_id != self.provider_instance_id
            ):
                raise ValueError(
                    "Bootstrap revocation requires this provider instance's fence receipt"
                )
        elif self.provider_fence is not None:
            raise ValueError("Only fenced bootstrap revocation accepts a provider fence")
        return self


class HostTaskObservation(ContractModel):
    schema_version: Literal["0.1"] = "0.1"
    task_id: str = Field(pattern=r"^host-[a-f0-9]{24}$")
    input_sha256: Sha256
    status: Literal["queued", "running", "completed", "failed"]
    job_uid: NonEmpty | None = None
    stage: NonEmpty
    observed_at: Timestamp
    node_name: NonEmpty | None = None
    node_uid: NonEmpty | None = None
    public_wireguard_key: NonEmpty | None = None
    registry_revision: PositiveInt
    host_ready: bool = False
    wireguard_ready: bool = False
    kubernetes_ready: bool = False
    cilium_ready: bool = False
    connectivity_verified: bool = False
    gpu_canary_verified: bool = False
    evidence_sha256: Sha256 | None = None
    gpu_runtime_ready: bool = False
    gpu_observation: GPUCapacityObservation | None = None
    cleanup_pending: bool = False
    bootstrap_fence_permitted: bool = False
    bootstrap_cleanup_complete: bool = False
    error: ErrorInfo | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_receipt(self, handler):
        value = handler(self)
        if self.gpu_observation is None:
            value.pop("gpu_observation", None)
        return value

    @model_validator(mode="after")
    def consistent_observation(self) -> Self:
        if self.status == "completed" and self.job_uid is None:
            raise ValueError("Completed host execution requires its observed Job UID")
        if self.status == "failed" and self.error is None:
            raise ValueError("Failed host execution requires an error")
        if self.status != "failed" and self.error is not None:
            raise ValueError("Non-failed execution cannot report a terminal error")
        if (self.node_name is None) != (self.node_uid is None):
            raise ValueError("Observed node name and UID must be supplied together")
        if self.gpu_runtime_ready and not (
            self.host_ready
            and self.wireguard_ready
            and self.kubernetes_ready
            and self.cilium_ready
            and self.connectivity_verified
            and self.gpu_canary_verified
            and self.evidence_sha256 is not None
            and self.node_uid is not None
        ):
            raise ValueError("GPU readiness requires actual host/network/node readiness")
        if self.status in {"queued", "running"} and self.gpu_runtime_ready:
            raise ValueError("In-flight host work cannot claim completed GPU verification")
        if self.gpu_observation is not None and (
            self.node_uid is None or self.gpu_observation.observed_at > self.observed_at
        ):
            raise ValueError("GPU observations require a bound node and an ordered timestamp")
        if (self.bootstrap_fence_permitted or self.bootstrap_cleanup_complete) and (
            self.status != "completed" or self.evidence_sha256 is None or self.gpu_runtime_ready
        ):
            raise ValueError("Bootstrap cleanup needs completed, non-serving evidence")
        if self.public_wireguard_key is not None:
            try:
                valid = len(base64.b64decode(self.public_wireguard_key, validate=True)) == 32
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("Malformed public WireGuard key")
        return self


def validate_host_followup(
    task: HostTask, previous: HostTask, observed: HostTaskObservation
) -> bool:
    """Validate a trusted chain; return whether this is a fresh failed-task recovery."""
    recovery = observed.status == "failed" and (
        (
            task.action == "cleanup-check"
            and previous.action in {"prepare", "join", "inspect", "cleanup-check"}
        )
        or (task.action == previous.action and task.action in {"remove", "revoke-bootstrap"})
        or (
            task.action == "remove"
            and previous.action == "inspect"
            and previous.node_uid is not None
        )
    )
    if (
        task.previous_task_id != previous.id
        or observed.task_id != previous.id
        or (observed.status != "completed" and not recovery)
        or any(
            getattr(task, field) != getattr(previous, field)
            for field in (
                "provider_instance_id",
                "node_name",
                "public_ip",
                "private_ip",
                "wireguard_ip",
                "trusted_host_key",
            )
        )
    ):
        raise ValueError("Host follow-up differs from its completed trusted task chain")
    known_uid = observed.node_uid or previous.node_uid
    if known_uid and task.node_uid != known_uid:
        raise ValueError("Host follow-up changed the observed Kubernetes UID")
    permitted = {
        "prepare": {"join", "cleanup-check"},
        "join": {"inspect", "remove", "cleanup-check"},
        "inspect": {"inspect", "remove", "cleanup-check"},
        "remove": {"remove"},
        "cleanup-check": {"cleanup-check", "revoke-bootstrap"},
        "revoke-bootstrap": {"revoke-bootstrap"},
    }
    if task.action not in permitted[previous.action]:
        raise ValueError("Invalid host lifecycle transition")
    if task.action == "revoke-bootstrap" and not (
        (previous.action == "cleanup-check" and observed.bootstrap_fence_permitted)
        or (recovery and previous.provider_fence == task.provider_fence)
    ):
        raise ValueError("Fenced cleanup requires the completed bootstrap safety check")
    return recovery
