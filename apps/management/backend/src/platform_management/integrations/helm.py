from typing import Protocol

from platform_contracts import ServingProfile


class ServingAdapter(Protocol):
    """C owns dynamic model-pool releases; B owns static platform releases."""

    def deploy(self, identity: str, profile: ServingProfile, operation_key: str) -> str: ...
    def observe(self, handle: str) -> str: ...


class SimulatedServingAdapter:
    def deploy(self, identity, profile, operation_key):
        return f"simulated-{identity}"

    def observe(self, handle):
        return "ready"
