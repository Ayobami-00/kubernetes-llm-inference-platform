import json

import pytest
from platform_contracts import Admission, PreparedEntry, RunSnapshot, validate_for_execution
from workload_runner.adapters.engine import build_engine_inputs
from workload_runner.artifacts import artifact_path, read_jsonl
from workload_runner.datasets.preparation import prepare_customer
from workload_runner.datasets.public import DownloadBudget, otel_messages

SCENARIOS = [
    "writer-assist-baseline",
    "code-agent-baseline",
    "tutor-chat-baseline",
    "doc-flow-baseline",
    "research-agent-baseline",
    "shared-contention",
    "platform-mixed",
]


@pytest.mark.parametrize("name", SCENARIOS)
def test_all_scenarios_compile_deterministically(name, compile_scenario, tmp_path):
    snapshot = compile_scenario(name)
    original = snapshot.model_dump_json()
    second = compile_scenario(name)
    assert second.model_dump_json() == original
    schedule = list(read_jsonl(artifact_path(snapshot.admission_schedule.uri), Admission))
    assert len(schedule) == 24 * len(snapshot.customers)
    assert len({(a.customer_ref, a.entry_id) for a in schedule}) == len(schedule)
    assert all(c.slo.window_s == 1 for c in snapshot.customers)
    assert snapshot.scenario.measurement_duration_s == 3
    assert all("smoke" in c.id for c in snapshot.customers)
    with pytest.raises(ValueError, match="preflight"):
        validate_for_execution(snapshot)
    context = build_engine_inputs(snapshot, tmp_path / name)
    rows = list(read_jsonl(tmp_path / name / "engine-inputs.jsonl"))
    assert set(context["sessions"]) == {a.id for a in schedule}
    assert all("references" not in json.dumps(row["turns"]) for row in rows)
    if name == "platform-mixed":
        assert {t["payload"]["stream"] for row in rows for t in row["turns"]} == {True, False}
        medium = [b.deployment_id for b in snapshot.bindings if "medium" in b.serving_profile_ref]
        assert medium == ["shared-medium", "shared-medium"]


def test_canonical_scenario_unchanged_and_exhaustion(catalog, prepared, compile_scenario):
    before = catalog.get("scenario", "writer-assist-baseline@1").model_dump_json()
    with pytest.raises(ValueError, match="exhaustion"):
        compile_scenario(smoke=False)
    compile_scenario()
    assert catalog.get("scenario", "writer-assist-baseline@1").model_dump_json() == before


def test_no_overwrite_on_conflicting_plan(compile_scenario, tmp_path):
    directory = tmp_path / "immutable"
    compile_scenario(output=directory)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    with pytest.raises(ValueError, match="different immutable"):
        compile_scenario(preset="reject", output=directory)
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}


@pytest.mark.parametrize(
    "customer", ["writer-assist", "code-agent", "tutor-chat", "doc-flow", "research-agent"]
)
def test_preparation_identities_and_disjointness(customer, prepared, catalog, tmp_path):
    manifest = prepared[customer + "@1"]
    again = prepare_customer(catalog, customer + "@1", tmp_path / "another", limit=100)
    assert manifest.id == again.id
    assert manifest.artifact.sha256 == again.artifact.sha256
    assert manifest.token_accounting == "emulated"
    entries = list(
        read_jsonl(artifact_path(manifest.artifact.uri, manifest.artifact.sha256), PreparedEntry)
    )
    warm = {e.source_id for e in entries if e.warmup}
    scored = {e.source_id for e in entries if not e.warmup}
    assert warm and scored and not warm & scored
    assert all(e.source_id.startswith("synthetic:") for e in entries)
    if customer == "code-agent":
        assert manifest.adaptations["output_cap"] == 200
        assert entries[0].turns[0].source_controls["max_tokens"] == 4096
        assert entries[0].turns[0].controls["max_tokens"] == 2048
        assert entries[0].context_mode == "recorded"


def test_corrupt_artifact_and_snapshot_independence(prepared, compile_scenario, catalog):
    snapshot = compile_scenario()
    before = snapshot.model_dump_json()
    catalog.get("customer", "writer-assist@1").notes = "edited source"
    assert snapshot.model_dump_json() == before
    RunSnapshot.model_validate_json(before)
    path = artifact_path(prepared["writer-assist@1"].artifact.uri)
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="checksum"):
        compile_scenario()


def test_bounds_and_trace_message_conversion(tmp_path):
    metadata = {"bytes": 0}
    budget = DownloadBudget(tmp_path, 5, metadata)
    budget.account(5)
    with pytest.raises(ValueError, match="bound exceeded"):
        budget.account(1)
    budget.client.close()
    result = otel_messages(
        [
            {
                "role": "assistant",
                "parts": [
                    {"type": "text", "content": "inspect"},
                    {"type": "tool_call", "id": "x", "name": "read", "arguments": {"file": "a"}},
                ],
            }
        ]
    )
    assert result[0]["content"] == "inspect"
    assert json.loads(result[0]["tool_calls"][0]["function"]["arguments"]) == {"file": "a"}
