"""CPU-only real HTTP/IPC acceptance; opt in with RUN_AIPERF_INTEGRATION=1."""

import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from jsonschema import Draft202012Validator
from test_managed import handoff
from workload_runner.artifacts import read_jsonl
from workload_runner.control import LocalRunnerAdapter, execute, initialize_attempt
from workload_runner.managed import execute_manifest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_AIPERF_INTEGRATION") != "1",
    reason="real AIPerf/HTTP integration is explicitly enabled",
)


@pytest.fixture(scope="module")
def endpoint():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "inference_stub.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--timeout-graceful-shutdown",
            "2",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx.get(url + "/health", timeout=1, trust_env=False).is_success:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise AssertionError("stub failed to start")
        yield url + "/v1/chat/completions"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize(
    "scenario",
    [
        "writer-assist-baseline",
        "code-agent-baseline",
        "tutor-chat-baseline",
        "doc-flow-baseline",
        "research-agent-baseline",
        "shared-contention",
        "platform-mixed",
    ],
)
def test_all_synthetic_paths(scenario, compile_scenario, tmp_path, endpoint, repo):
    snapshot = compile_scenario(scenario)
    snapshot.target.endpoint = endpoint
    directory = tmp_path / "execution"
    manifest = handoff(snapshot, directory)
    before = manifest.with_name("snapshot.json").read_bytes()
    run = execute_manifest(manifest)
    assert run.id == "run-management-test"
    assert manifest.with_name("snapshot.json").read_bytes() == before
    assert manifest.with_name("result.json").exists()
    assert json.loads(manifest.with_name("observation.json").read_text())["status"] == "completed"
    assert run.status == "completed", run.error
    assert run.result.requests.not_sent == 0
    assert run.result.requests.failed == 0
    assert run.result.requests.completed > 0
    assert (directory / "aiperf/profile_export_raw.jsonl").exists()
    assert (directory / "report.md").exists()
    for schema, filename in [
        ("run", "run.json"),
        ("request-record", "requests.jsonl"),
        ("session-record", "sessions.jsonl"),
    ]:
        validator = Draft202012Validator(
            json.loads((repo / "contracts/schemas" / f"{schema}.schema.json").read_text())
        )
        items = (
            [json.loads((directory / filename).read_text())]
            if filename.endswith(".json")
            else read_jsonl(directory / filename)
        )
        for item in items:
            validator.validate(item)


@pytest.mark.parametrize(
    ("preset", "outcome"),
    [
        ("reject", "rejected"),
        ("error", "failed"),
        ("timeout", "timed_out"),
        ("slow-first", "timed_out"),
        ("slow-completion", "timed_out"),
        ("interrupted", "failed"),
    ],
)
def test_native_failures_and_session_abort(preset, outcome, compile_scenario, endpoint, tmp_path):
    snapshot = compile_scenario("tutor-chat-baseline", preset=preset)
    snapshot.target.endpoint = endpoint
    directory = tmp_path / "execution"
    initialize_attempt(snapshot, directory)
    run = execute(directory)
    assert run.result is not None, run.error
    records = list(read_jsonl(directory / "requests.jsonl"))
    assert any(r["outcome"] == outcome for r in records)
    assert not any(r["complete_output"] for r in records)
    assert all(len(s["request_ids"]) <= 1 for s in read_jsonl(directory / "sessions.jsonl"))
    assert run.result.slo_verdict != "passing"
    if preset in {"reject", "error", "interrupted"}:
        assert run.status == "completed"
        assert run.result.validity == "valid"
        assert run.result.slo_verdict == "violating"


def test_cancel_keeps_partial_artifacts_and_idempotent_attempt(
    compile_scenario, endpoint, tmp_path
):
    snapshot = compile_scenario("platform-mixed", preset="timeout")
    snapshot.target.endpoint = endpoint
    adapter = LocalRunnerAdapter(tmp_path / "attempts")
    handle = adapter.launch(snapshot, "cancel-example")
    from pathlib import Path

    for _ in range(600):
        if (Path(handle) / "engine-clock.json").exists():
            break
        time.sleep(0.05)
    adapter.stop(handle)
    for _ in range(600):
        observation = adapter.observe(handle)
        if observation.status not in {"queued", "running"}:
            break
        time.sleep(0.05)
    assert observation.status == "cancelled"
    assert (Path(handle) / "requests.jsonl").exists()
    assert adapter.launch(snapshot, "cancel-example") == handle


def test_drain_retires_delayed_calls_without_http(
    catalog, prepared, endpoint, tmp_path, monkeypatch
):
    from platform_contracts import ExecutionTarget
    from workload_runner.datasets import preparation
    from workload_runner.scenario_loader import compile_run

    original = preparation.synthetic_entries

    def entries(customer, count):
        for row in original(customer, count):
            row["delays"] = [0.0, 10000.0]
            yield row

    monkeypatch.setattr(preparation, "synthetic_entries", entries)
    prepared["code-agent@1"] = preparation.prepare_customer(
        catalog, "code-agent@1", tmp_path / "delayed", limit=100
    )
    snapshot = compile_run(
        catalog,
        "code-agent-baseline@1",
        prepared,
        ExecutionTarget(mode="local-stub", endpoint=endpoint),
        tmp_path / "plan",
        smoke=True,
    )
    directory = tmp_path / "execution"
    initialize_attempt(snapshot, directory)
    run = execute(directory)
    assert run.status == "completed", run.error
    assert run.result.requests.not_sent == 0
    assert set(run.result.invalid_reasons) <= {"excessive_scheduling_lag"}
    assert run.result.requests.attempted == 24
    assert run.result.customer_sessions[snapshot.customers[0].ref].cutoff == 24
    assert all(record["turn_index"] == 0 for record in read_jsonl(directory / "requests.jsonl"))


def test_large_payload_headers_and_secret_redaction(
    catalog, prepared, endpoint, tmp_path, monkeypatch
):
    from platform_contracts import ExecutionTarget
    from workload_runner.datasets import preparation
    from workload_runner.scenario_loader import compile_run

    original = preparation.synthetic_entries

    def entries(customer, count):
        for row in original(customer, count):
            row["turns"][0][0][-1]["content"] += " context" * 1000
            yield row

    monkeypatch.setattr(preparation, "synthetic_entries", entries)
    prepared["doc-flow@1"] = preparation.prepare_customer(
        catalog, "doc-flow@1", tmp_path / "large", limit=100
    )
    secret = "stream-a-private-test-credential-7e213"
    monkeypatch.setenv("STREAM_A_TEST_KEY", secret)
    snapshot = compile_run(
        catalog,
        "doc-flow-baseline@1",
        prepared,
        ExecutionTarget(
            mode="local-stub", endpoint=endpoint, credential_env={"doc-flow": "STREAM_A_TEST_KEY"}
        ),
        tmp_path / "plan",
        smoke=True,
    )
    directory = tmp_path / "execution"
    initialize_attempt(snapshot, directory)
    run = execute(directory)
    assert run.status == "completed", run.error
    assert run.result.requests.completed == 24
    assert all(
        json.loads(r["output_text"])["clause"] for r in read_jsonl(directory / "requests.jsonl")
    )
    for path in directory.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".jsonl", ".log", ".yaml", ".csv", ".txt"}:
            assert secret not in path.read_text(), path


def test_overloaded_generator_is_invalid(compile_scenario, endpoint, tmp_path):
    snapshot = compile_scenario("tutor-chat-baseline", preset="timeout")
    snapshot.target.endpoint = endpoint
    snapshot.customers[0].limits.max_inflight_requests = 1
    snapshot.customers[0].reserved_concurrency = 1
    directory = tmp_path / "execution"
    initialize_attempt(snapshot, directory)
    run = execute(directory)
    assert run.result.validity == "invalid"
    assert "missed_due_admission" in run.result.invalid_reasons
    assert run.result.slo_verdict == "not_evaluated"
