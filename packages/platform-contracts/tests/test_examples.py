import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from platform_contracts import (
    InfrastructureInventory,
    OperationStatus,
    Run,
    WorkerGroup,
    build_run_snapshot,
    load_document,
    validate_for_execution,
)
from platform_contracts.loading import CONFIG_FOLDERS, CONFIG_MODELS

ROOT = Path(__file__).resolve().parents[3]
RUNTIME_MODELS = {
    "runs": ("run", Run),
    "worker-groups": ("worker-group", WorkerGroup),
    "operations": ("operation", OperationStatus),
    "inventories": ("inventory", InfrastructureInventory),
}
RUNTIME_FILES = sorted((ROOT / "contracts/fixtures").rglob("*.json"))
CONFIG_FILES = [
    (kind, path)
    for kind, folder in CONFIG_FOLDERS.items()
    for path in sorted((ROOT / folder).glob("*.yaml"))
]


def schema_validator(name):
    schema = json.loads((ROOT / "contracts/schemas" / f"{name}.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@pytest.mark.parametrize("kind,path", CONFIG_FILES, ids=[path.stem for _, path in CONFIG_FILES])
def test_authored_examples_match_schemas(kind, path):
    spec = load_document(path, CONFIG_MODELS[kind])
    schema_name = "worker-group-spec" if kind == "worker-group" else kind
    schema_validator(schema_name).validate(spec.model_dump(mode="json"))


@pytest.mark.parametrize("path", RUNTIME_FILES, ids=lambda path: str(path.relative_to(ROOT)))
def test_every_runtime_fixture_roundtrips_and_matches_schema(path):
    kind = path.relative_to(ROOT / "contracts/fixtures").parts[0]
    schema_name, model = RUNTIME_MODELS[kind]
    document = json.loads(path.read_text())
    record = model.model_validate(document)
    assert record.fixture is True
    assert model.model_validate_json(record.model_dump_json()) == record
    schema_validator(schema_name).validate(document)


def test_all_five_profiles_and_seven_scenarios_resolve(catalog, mixed_snapshot):
    assert {ref.split("@")[0] for ref in catalog.records["customer"]} == {
        "writer-assist",
        "code-agent",
        "tutor-chat",
        "doc-flow",
        "research-agent",
    }
    assert {kind: len(records) for kind, records in catalog.records.items()} == {
        "customer": 5,
        "scenario": 7,
        "worker-group": 3,
        "serving-profile": 4,
        "dataset-manifest": 6,
    }
    for scenario in catalog.records["scenario"].values():
        bindings = tuple(
            b for b in mixed_snapshot.bindings if b.customer_ref in scenario.participants
        )
        snapshot = build_run_snapshot(catalog, scenario.ref, bindings)
        assert snapshot.scenario.measurement_duration_s == 600
        assert snapshot.scenario.warmup.duration_s == 60
        assert snapshot.scenario.drain_timeout_s == 120
        assert snapshot.scenario.seed == 42
        for customer in snapshot.customers:
            phases = snapshot.scenario.phases
            assert phases[1].arrivals[customer.id].rate == phases[0].arrivals[customer.id].rate * 3
        with pytest.raises(ValueError, match="missing prepared dataset"):
            validate_for_execution(snapshot)


def test_shared_customers_use_one_medium_deployment(mixed_snapshot):
    bindings = {b.customer_ref: b for b in mixed_snapshot.bindings}
    assert bindings["doc-flow@1"].deployment_id == bindings["research-agent@1"].deployment_id
    assert len({b.deployment_id for b in mixed_snapshot.bindings}) == 4


def test_completed_execution_does_not_imply_passing_slo(root):
    run = Run.model_validate_json(
        (root / "contracts/fixtures/runs/writer-assist/completed-violating.json").read_text()
    )
    assert run.status == "completed"
    assert run.result.validity == "valid"
    assert run.result.slo_verdict == "violating"


def test_fixture_sources_do_not_claim_prepared_benchmark_data(catalog):
    assert all(dataset.prepared is None for dataset in catalog.records["dataset-manifest"].values())
    code = catalog.get("customer", "code-agent@1")
    assert code.workload.preserve_recorded_context
    assert code.quality.evaluator == "not_evaluated"
