"""Public shared-contract interfaces. No service or infrastructure dependencies."""

from .customers import CustomerSpec
from .datasets import DatasetManifest
from .inventory import InfrastructureInventory
from .loading import ContractCatalog, load_catalog, load_document
from .operations import OperationStatus
from .runs import (
    ExecutionVersions,
    Run,
    RunResult,
    RunSnapshot,
    ServingBinding,
    build_run_snapshot,
)
from .scenarios import ScenarioSpec
from .serving import ServingProfile
from .validation import validate_catalog, validate_for_execution, validate_snapshot
from .worker_groups import WorkerGroup, WorkerGroupSpec, WorkerGroupStatus

__all__ = [
    "ContractCatalog",
    "CustomerSpec",
    "DatasetManifest",
    "ExecutionVersions",
    "InfrastructureInventory",
    "OperationStatus",
    "Run",
    "RunResult",
    "RunSnapshot",
    "ScenarioSpec",
    "ServingBinding",
    "ServingProfile",
    "WorkerGroup",
    "WorkerGroupSpec",
    "WorkerGroupStatus",
    "build_run_snapshot",
    "load_catalog",
    "load_document",
    "validate_catalog",
    "validate_for_execution",
    "validate_snapshot",
]
