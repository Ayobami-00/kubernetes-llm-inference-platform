from typing import Protocol


class HostAutomation(Protocol):
    """B supplies idempotent bootstrap/removal automation using credential references."""

    def prepare(self, instance_id: str, host_profile: str, operation_key: str) -> None: ...
    def remove(self, instance_id: str, operation_key: str) -> None: ...


class SimulatedHostAutomation:
    def prepare(self, instance_id, host_profile, operation_key):
        return None

    def remove(self, instance_id, operation_key):
        return None
