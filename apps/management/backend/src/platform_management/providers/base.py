from typing import Protocol


class TransientProviderError(Exception):
    pass


class AllocationAmbiguous(TransientProviderError):
    pass


class CapacityUnavailable(Exception):
    pass


class GPUProvider(Protocol):
    def allocate(self, intent_id: str) -> list[dict]: ...
    def reconcile(self, intent_id: str) -> list[dict] | None:
        """None means unresolved; an empty list means confirmed absence."""
        ...

    def terminate(self, instance_id: str, fault: str) -> None: ...
    def termination_confirmed(self, instance_id: str) -> bool: ...
