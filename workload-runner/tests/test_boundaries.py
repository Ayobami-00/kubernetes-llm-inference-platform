import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import httpx
import pytest
from platform_contracts import ExecutionTarget, RunSnapshot
from workload_runner.adapters.engine import build_engine_inputs, normalize, response_parts
from workload_runner.artifacts import atomic_json, read_jsonl, write_jsonl
from workload_runner.control import LocalRunnerAdapter, initialize_attempt, reassess
from workload_runner.datasets.preparation import TokenCounter, prepare_customer
from workload_runner.datasets.public import DownloadBudget, RangeFile
from workload_runner.reporting.quality import corpus_gleu, evaluate_quality


def test_actual_tokenizer_api_is_a_sequence():
    counter = TokenCounter()
    tokenizer = Mock()
    tokenizer.apply_chat_template.return_value = [1, 2, 3, 4, 5, 6, 7]
    counter.tokenizer = tokenizer
    assert counter.count([dict(role="user", content="test")]) == 7
    assert tokenizer.apply_chat_template.call_args.kwargs["return_dict"] is False


def test_endpoint_changes_do_not_resample_workload(catalog, prepared, tmp_path):
    from workload_runner.artifacts import artifact_path
    from workload_runner.scenario_loader import compile_run

    schedules = []
    for index, url in enumerate(
        ["http://localhost:8001/v1/chat/completions", "http://localhost:8002/v1/chat/completions"]
    ):
        snapshot = compile_run(
            catalog,
            "code-agent-baseline@1",
            prepared,
            ExecutionTarget(mode="local-stub", endpoint=url),
            tmp_path / f"plan-{index}",
            smoke=True,
        )
        schedules.append(
            [
                (row["scheduled_offset_ms"], row["entry_id"], row["warmup"])
                for row in read_jsonl(artifact_path(snapshot.admission_schedule.uri))
            ]
        )
    assert schedules[0] == schedules[1]


def test_gleu_pinned_upstream_golden():
    from pathlib import Path

    golden = json.loads((Path(__file__).parent / "fixtures/gleu-reference.json").read_text())
    assert corpus_gleu(golden["examples"]) == pytest.approx(
        golden["expected_corpus_gleu"], abs=golden["tolerance"]
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://name:secret@example.com/v1/chat/completions",
        "https://example.com/v1/chat/completions?api_key=secret",
    ],
)
def test_no_url_credentials(url):
    with pytest.raises(ValueError, match="environment references"):
        ExecutionTarget(mode="benchmark", endpoint=url)


def test_concurrent_launch_and_repeat_without_preflight(compile_scenario, tmp_path, monkeypatch):
    snapshot = compile_scenario()
    preflight = Mock()
    monkeypatch.setattr("workload_runner.control.preflight", preflight)
    popen = Mock(return_value=Mock(pid=os.getpid()))
    monkeypatch.setattr("workload_runner.control.subprocess.Popen", popen)
    adapter = LocalRunnerAdapter(tmp_path / "attempts")
    with ThreadPoolExecutor(max_workers=4) as pool:
        handles = list(pool.map(lambda _: adapter.launch(snapshot, "same-key"), range(4)))
    assert len(set(handles)) == 1
    assert popen.call_count == preflight.call_count == 1
    preflight.side_effect = ValueError("credentials no longer exist")
    assert adapter.launch(snapshot, "same-key") == handles[0]


def test_preparation_reference_validation(compile_scenario):
    snapshot = compile_scenario()
    data = snapshot.model_dump(mode="json")
    data["preparation"].append(data["preparation"][0])
    with pytest.raises(ValueError, match="duplicate preparation"):
        RunSnapshot.model_validate(data)


@pytest.mark.parametrize("change", ["duplicate", "cohort", "missing", "phase"])
def test_schedule_validation(change, compile_scenario, tmp_path):
    snapshot = compile_scenario()
    from workload_runner.artifacts import artifact_path

    admissions = list(read_jsonl(artifact_path(snapshot.admission_schedule.uri)))
    if change == "duplicate":
        admissions.append(admissions[0])
    elif change == "cohort":
        admissions[0]["warmup"] = not admissions[0]["warmup"]
    elif change == "missing":
        admissions[0]["entry_id"] = "missing-entry"
    else:
        admissions[0]["phase"] = "recovery"
    path = tmp_path / "changed.jsonl"
    sha = write_jsonl(path, admissions)
    snapshot.admission_schedule.uri = path.as_uri()
    snapshot.admission_schedule.sha256 = sha
    with pytest.raises(ValueError):
        build_engine_inputs(snapshot, tmp_path / "engine")


def test_sse_tool_fragments_reassemble_and_control_chunks_are_ignored():
    def chunk(time, delta):
        return dict(perf_ns=time, data=dict(choices=[dict(delta=delta)]))

    result = response_parts(
        dict(
            responses=[
                chunk(10, {"tool_calls": [{"index": 0, "id": "call-id", "type": "function"}]}),
                chunk(
                    20,
                    {"tool_calls": [{"index": 0, "function": {"name": "read", "arguments": "{"}}]},
                ),
                chunk(30, {"tool_calls": [{"index": 0, "function": {"arguments": '"file":"a"}'}}]}),
            ]
        )
    )
    assert result[2] == 20
    assert result[1][0]["id"] == "call-id"
    assert json.loads(result[1][0]["function"]["arguments"]) == {"file": "a"}


def test_quality_coverage_includes_unscorable_formats(compile_scenario, tmp_path):
    from platform_contracts import RequestRecord
    from workload_runner.artifacts import artifact_path

    snapshot = compile_scenario("doc-flow-baseline")
    customer = snapshot.customers[0]
    entry = next(
        a["entry_id"]
        for a in read_jsonl(artifact_path(snapshot.admission_schedule.uri))
        if not a["warmup"]
    )
    records = [
        RequestRecord(
            run_id="test",
            request_id=f"r-{i}",
            customer_ref=customer.ref,
            source_entry_id=entry,
            session_id=f"s-{i}",
            turn_index=0,
            warmup_cohort=False,
            phase="measurement",
            actual_send_offset_ms=1000.0,
            outcome="completed",
            eligible_for_slo=True,
            complete_output=True,
            output_text=text,
        )
        for i, text in enumerate(
            ['{"clause":"Either party may terminate with thirty days notice."}', "bad JSON"]
        )
    ]
    results = evaluate_quality(snapshot, records, tmp_path)
    assert results[0].scores["response_format_attainment"] == 0.5
    coverage = json.loads((tmp_path / "quality.json").read_text())[customer.ref]["coverage"]
    assert coverage["eligible"] == sum(
        coverage[k] for k in ("evaluated", "failed", "missing", "unscorable")
    )


def test_range_reads_cache_and_bounds(tmp_path):
    data = b"PAR1example"
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(206, content=data, headers={"content-range": "bytes 0-10/11"})

    metadata = {"bytes": 0}
    budget = DownloadBudget(tmp_path, 100, metadata)
    budget.client.close()
    budget.client = httpx.Client(transport=httpx.MockTransport(respond))
    reader = RangeFile("https://example.com/data", budget, len(data))
    assert reader.read() == data
    again = RangeFile("https://example.com/data", budget, len(data))
    assert again.read() == data and len(calls) == 1
    assert metadata["bytes"] == len(data)
    budget.client.close()


def test_context_exclusion_is_not_silent_truncation(catalog, tmp_path, monkeypatch):
    from workload_runner.datasets import preparation

    original = preparation.synthetic_entries

    def examples(customer, count):
        for i, row in enumerate(original(customer, count + 1)):
            if i == 0:
                row["turns"][0][0][0]["content"] = "x" * 300000
            yield row

    monkeypatch.setattr(preparation, "synthetic_entries", examples)
    manifest = prepare_customer(catalog, "code-agent@1", tmp_path, limit=3)
    assert manifest.exclusions["preparation_character_bound_turns"] >= 1


def test_attempt_history_cannot_be_overwritten_and_checksums_are_enforced(
    compile_scenario, tmp_path
):
    snapshot = compile_scenario()
    directory = tmp_path / "attempt"
    initialize_attempt(snapshot, directory)
    with pytest.raises(ValueError, match="already exists"):
        initialize_attempt(snapshot, directory)
    build_engine_inputs(snapshot, directory)
    requests, sessions = normalize(snapshot, directory, "test")
    from slo_evaluator import evaluate

    run = json.loads((directory / "run.json").read_text())
    from workload_runner.artifacts import digest

    result = evaluate(
        snapshot,
        requests,
        sessions,
        artifacts={
            "requests": dict(
                uri=(directory / "requests.jsonl").as_uri(),
                sha256=digest((directory / "requests.jsonl").read_bytes()),
            )
        },
    )
    run.update(
        status="completed",
        started_at=run["created_at"],
        finished_at=run["created_at"],
        result=result.model_dump(mode="json"),
    )
    atomic_json(directory / "run.json", run)
    with (directory / "requests.jsonl").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="checksum"):
        reassess(directory)


def test_shared_deployment_queue():
    from inference_stub.app import StubSettings, create_app

    app = create_app(StubSettings(first_content_ms=20, slots_per_deployment=1))

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://stub"
        ) as client:

            async def send(task):
                return await client.post(
                    "/v1/chat/completions",
                    headers={"x-platform-task": task, "x-platform-deployment": "shared-medium"},
                    json={
                        "model": "general-medium",
                        "messages": [{"role": "user", "content": "fixture"}],
                    },
                )

            responses = await asyncio.gather(send("extraction"), send("research"))
            assert all(r.status_code == 200 for r in responses)
            state = (await client.get("/debug/state")).json()["queues"]
            assert len(state) == 1 and state[0]["maximum_active"] == 1
            assert state[0]["pending"] == state[0]["active"] == 0

    asyncio.run(run())


def test_startup_cancellation_has_partial_records_without_launch(
    compile_scenario, tmp_path, monkeypatch
):
    from workload_runner.control import execute

    snapshot = compile_scenario()
    directory = tmp_path / "cancelled-before-start"
    initialize_attempt(snapshot, directory)
    (directory / "cancel-requested").touch()
    launch = Mock(side_effect=AssertionError("cancelled attempt must not launch HTTP engine"))
    monkeypatch.setattr("workload_runner.control.subprocess.Popen", launch)
    run = execute(directory)
    assert run.status == "cancelled" and run.error is None
    assert run.result.validity == "invalid"
    assert (directory / "requests.jsonl").is_file()
    launch.assert_not_called()


def test_launch_recovers_interrupted_creation_as_failed(compile_scenario, tmp_path, monkeypatch):
    snapshot = compile_scenario()
    monkeypatch.setattr("workload_runner.control.preflight", lambda s: None)
    popen = Mock(return_value=Mock(pid=os.getpid()))
    monkeypatch.setattr("workload_runner.control.subprocess.Popen", popen)
    adapter = LocalRunnerAdapter(tmp_path / "attempts")
    handle = adapter.launch(snapshot, "partial-launch")
    from pathlib import Path

    (Path(handle) / "run.json").unlink()
    assert adapter.launch(snapshot, "partial-launch") == handle
    observation = adapter.observe(handle)
    assert observation.status == "failed" and observation.error.code == "interrupted_launch"
    assert popen.call_count == 1


def test_execution_initialization_keeps_existing_snapshot_bytes(compile_scenario, tmp_path):
    snapshot = compile_scenario()
    directory = tmp_path / "same-input-and-output"
    directory.mkdir()
    raw = snapshot.model_dump_json()
    (directory / "snapshot.json").write_text(raw)
    initialize_attempt(snapshot, directory)
    assert (directory / "snapshot.json").read_text() == raw


def test_management_observation_shape_compatibility():
    from platform_contracts import RunnerObservation

    integration = pytest.importorskip("platform_management.integrations.runner")
    for status in ("queued", "running", "completed", "failed", "cancelled"):
        observation = RunnerObservation(
            status=status, stage="measuring" if status == "running" else None
        )
        compatible = integration.RunnerObservation.model_validate(
            observation.model_dump(mode="json")
        )
        assert compatible.model_dump(mode="json") == observation.model_dump(mode="json")
