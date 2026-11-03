import json
import os
from unittest.mock import Mock

import pytest
from platform_contracts import Run, RunnerObservation
from workload_runner.adapters.engine import normalize, response_parts
from workload_runner.artifacts import atomic_json, write_jsonl
from workload_runner.control import LocalRunnerAdapter, initialize_attempt, reassess


def test_sse_controls_are_not_first_content():
    responses = [
        dict(
            perf_ns=10,
            packets=[
                dict(name="data", value=json.dumps({"choices": [{"delta": {"role": "assistant"}}]}))
            ],
        ),
        dict(
            perf_ns=20,
            packets=[
                dict(name="data", value=json.dumps({"choices": [{"delta": {"content": "hello"}}]}))
            ],
        ),
        dict(
            perf_ns=30,
            packets=[
                dict(
                    name="data",
                    value=json.dumps(
                        {
                            "choices": [{"delta": {}, "finish_reason": "stop"}],
                            "usage": {"completion_tokens": 1},
                        }
                    ),
                )
            ],
        ),
        dict(perf_ns=40, packets=[dict(name="data", value="[DONE]")]),
    ]
    assert response_parts(dict(responses=responses)) == (
        "hello",
        [],
        20,
        "stop",
        {"completion_tokens": 1},
    )


def test_idempotent_launch_conflicting_inputs_and_lost_attempt(
    compile_scenario, tmp_path, monkeypatch
):
    snapshot = compile_scenario()
    monkeypatch.setattr("workload_runner.control.preflight", lambda s: None)
    process = Mock(pid=os.getpid())
    popen = Mock(return_value=process)
    monkeypatch.setattr("workload_runner.control.subprocess.Popen", popen)
    adapter = LocalRunnerAdapter(tmp_path / "executions")
    handle = adapter.launch(snapshot, "same-operation")
    assert adapter.launch(snapshot, "same-operation") == handle
    assert popen.call_count == 1
    assert adapter.observe(handle).status == "queued"
    changed = snapshot.model_copy(deep=True)
    changed.target.stub_preset = "error"
    with pytest.raises(ValueError, match="different inputs"):
        adapter.launch(changed, "same-operation")
    adapter.stop(handle)
    directory = adapter._directory(handle)
    assert (directory / "cancel-requested").exists()
    atomic_json(directory / "heartbeat.json", dict(pid=99999999, at="2000-01-01T00:00:00+00:00"))
    observation = adapter.observe(handle)
    assert observation.status == "failed" and observation.error.code == "lost_attempt"
    RunnerObservation.model_validate_json(observation.model_dump_json())
    assert adapter.launch(snapshot, "same-operation") == handle
    assert popen.call_count == 1


def test_normalization_missing_admissions_and_immutable_reassessment(compile_scenario, tmp_path):
    from slo_evaluator import evaluate
    from workload_runner.adapters.engine import build_engine_inputs

    snapshot = compile_scenario()
    directory = tmp_path / "attempt"
    run = initialize_attempt(snapshot, directory)
    build_engine_inputs(snapshot, directory)
    requests, sessions = normalize(snapshot, directory, run.id)
    assert len(requests) == 24
    assert all(r.outcome == "not_sent" for r in requests)
    from workload_runner.control import result_artifacts

    result = evaluate(snapshot, requests, sessions, artifacts=result_artifacts(directory))
    assert result.validity == "invalid" and result.slo_verdict == "not_evaluated"
    data = run.model_dump(mode="json")
    data.update(
        status="completed",
        started_at=run.created_at,
        finished_at=run.created_at,
        result=result.model_dump(mode="json"),
    )
    atomic_json(directory / "run.json", Run.model_validate(data))
    before = (directory / "run.json").read_bytes()
    output = reassess(directory)
    assert (output / "result.json").is_file()
    assert (directory / "run.json").read_bytes() == before


def test_atomic_write_retains_complete_old_file_on_failure(tmp_path):
    path = tmp_path / "observation.json"
    atomic_json(path, {"status": "queued"})
    with pytest.raises(ValueError):
        atomic_json(path, {"value": float("nan")})
    assert json.loads(path.read_text()) == {"status": "queued"}
    write_jsonl(tmp_path / "records.jsonl", [{"a": 1}])
