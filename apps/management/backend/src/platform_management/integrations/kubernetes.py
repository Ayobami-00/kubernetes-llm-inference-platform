from typing import Protocol


class ClusterAutomation(Protocol):
    def join(self, instance_id: str, operation_key: str) -> tuple[str, str]: ...
    def drain(self, node_uid: str, timeout_s: int, operation_key: str) -> None: ...


class SimulatedClusterAutomation:
    def join(self, instance_id, operation_key):
        return f"node-{instance_id}", f"uid-{instance_id}"

    def drain(self, node_uid, timeout_s, operation_key):
        return None
