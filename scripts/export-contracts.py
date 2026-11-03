#!/usr/bin/env python3
"""Export deterministic, self-contained schemas, or check committed exports without writing."""

import argparse
import json
from pathlib import Path

from platform_contracts import (
    Admission,
    CustomerSpec,
    DatasetManifest,
    ExecutionTarget,
    InfrastructureInventory,
    OperationStatus,
    PreparationManifest,
    PreparedEntry,
    RequestRecord,
    Run,
    RunnerObservation,
    RunResult,
    RunSnapshot,
    ScenarioSpec,
    ServingProfile,
    SessionRecord,
    WorkerGroup,
    WorkerGroupSpec,
    WorkerGroupStatus,
)

SCHEMA_MODELS = {
    "customer": CustomerSpec,
    "scenario": ScenarioSpec,
    "run": Run,
    "run-snapshot": RunSnapshot,
    "run-result": RunResult,
    "worker-group": WorkerGroup,
    "worker-group-spec": WorkerGroupSpec,
    "worker-group-status": WorkerGroupStatus,
    "serving-profile": ServingProfile,
    "operation": OperationStatus,
    "inventory": InfrastructureInventory,
    "dataset-manifest": DatasetManifest,
    "execution-target": ExecutionTarget,
    "preparation-manifest": PreparationManifest,
    "prepared-entry": PreparedEntry,
    "admission": Admission,
    "request-record": RequestRecord,
    "session-record": SessionRecord,
    "runner-observation": RunnerObservation,
}


def schema_documents() -> dict[str, str]:
    documents = {}
    for name, model in sorted(SCHEMA_MODELS.items()):
        filename = f"{name}.schema.json"
        schema = model.model_json_schema(mode="validation")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://schemas.inference-platform.example/0.1/{filename}"
        documents[filename] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    return documents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report drift without changing files")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "contracts" / "schemas",
    )
    args = parser.parse_args()
    documents = schema_documents()
    existing = {path.name for path in args.output_dir.glob("*.schema.json")}
    stale = existing - documents.keys()
    drift = sorted(
        stale
        | {
            filename
            for filename, text in documents.items()
            if not (args.output_dir / filename).exists()
            or (args.output_dir / filename).read_text() != text
        }
    )
    if args.check:
        if drift:
            print("Schema drift: " + ", ".join(drift))
            return 1
        print(f"All {len(documents)} schemas are current.")
        return 0
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for filename in stale:
        (args.output_dir / filename).unlink()
    for filename, text in documents.items():
        (args.output_dir / filename).write_text(text)
    print(f"Exported {len(documents)} schemas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
