"""Durable local process adapter implementing management's launch/stop/observe protocol.

The filesystem is the authority for an attempt. A dead process is never relaunched.
Remote management should mount durable artifact storage and treat handles as opaque.
"""

import fcntl
import importlib.metadata
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from platform_contracts import Run, RunnerObservation, RunSnapshot, validate_for_execution

from .adapters.engine import build_engine_inputs, normalize
from .artifacts import artifact_path, atomic_json, canonical, digest, read_jsonl
from .reporting.quality import evaluate_quality
from .reporting.report import write_report
from .security import credential_values, redact, scrub_artifacts


def utcnow():
    return datetime.now(UTC).isoformat()


def preflight(snapshot):
    snapshot = RunSnapshot.model_validate_json(snapshot.model_dump_json())
    if snapshot.target is None or snapshot.admission_schedule is None or not snapshot.preparation:
        raise ValueError("a compiled execution target, schedule and prepared inputs are required")
    artifact_path(snapshot.admission_schedule.uri, snapshot.admission_schedule.sha256)
    for manifest in snapshot.preparation:
        artifact_path(manifest.artifact.uri, manifest.artifact.sha256)
    if snapshot.target.mode == "benchmark":
        validate_for_execution(snapshot)
        if any(
            p.data_kind != "public"
            or p.token_accounting != "model_tokenizer"
            or any(value is None for value in p.licenses.values())
            for p in snapshot.preparation
        ):
            raise ValueError(
                "benchmark preparation needs public inputs, real tokenization and reviewed licenses"
            )
    for variable in snapshot.target.credential_env.values():
        if not os.environ.get(variable):
            raise ValueError(f"missing credential environment reference: {variable}")
    for value in credential_values(snapshot):
        if value in snapshot.model_dump_json() or any(
            value.encode() in artifact_path(p.artifact.uri, p.artifact.sha256).read_bytes()
            for p in snapshot.preparation
        ):
            raise ValueError(
                "frozen inputs contain an endpoint credential value; create clean inputs"
            )
    try:
        version = importlib.metadata.version("aiperf")
    except importlib.metadata.PackageNotFoundError:
        version = None
    if version != "0.13.0":
        raise ValueError("execution requires the pinned benchmark extra: aiperf==0.13.0")


def initialize_attempt(snapshot, directory, *, run_id=None):
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "run.json").exists():
        raise ValueError(
            "execution attempt already exists; historical inputs cannot be overwritten"
        )
    run = Run(
        id=run_id or "attempt-" + digest(str(directory.resolve()).encode())[:24],
        fixture=snapshot.target.mode == "local-stub",
        status="queued",
        created_at=utcnow(),
        snapshot=snapshot,
    )
    snapshot_path = directory / "snapshot.json"
    if snapshot_path.exists():
        existing = RunSnapshot.model_validate_json(snapshot_path.read_text())
        if canonical(existing) != canonical(snapshot):
            raise ValueError("attempt directory contains different immutable snapshot inputs")
    else:
        atomic_json(snapshot_path, snapshot)
    atomic_json(directory / "preparation.json", snapshot.preparation)
    atomic_json(directory / "heartbeat.json", dict(pid=os.getpid(), at=utcnow()))
    atomic_json(directory / "run.json", run)
    return run


def load_run(directory):
    return Run.model_validate_json((directory / "run.json").read_text())


def result_artifacts(directory):
    filenames = {
        "requests": "requests.jsonl",
        "sessions": "sessions.jsonl",
        "quality": "quality.json",
        "responses": "responses.jsonl",
        "schedule": "admission-schedule.jsonl",
        "snapshot": "snapshot.json",
        "preparation": "preparation.json",
        "clock": "engine-clock.json",
        "report": "report.md",
        "engine-log": "engine.log",
        "readiness": "readiness.json",
    }
    raw = directory / "aiperf"
    if raw.is_dir():
        atomic_json(
            directory / "raw-index.json",
            {
                str(path.relative_to(directory)): digest(path.read_bytes())
                for path in sorted(raw.rglob("*"))
                if path.is_file()
            },
        )
        filenames["raw-index"] = "raw-index.json"
    artifacts = {
        name: dict(
            uri=(directory / filename).as_uri(), sha256=digest((directory / filename).read_bytes())
        )
        for name, filename in filenames.items()
        if (directory / filename).is_file()
    }
    if raw.is_dir():
        for index, path in enumerate(sorted(raw.rglob("*"))):
            if path.is_file():
                artifacts[f"raw-{index}"] = dict(
                    uri=path.as_uri(), sha256=digest(path.read_bytes())
                )
    return artifacts


def engine_completed(directory, returncode, requests):
    if returncode == 0:
        return True
    if not requests or any(r.outcome == "not_sent" for r in requests):
        return False
    # Pinned AIPerf treats an entirely unsuccessful inference workload as an
    # export error. Completed client attempts still have valid SLO observations.
    log = (directory / "engine.log").read_text()
    return "All results received, initiating shutdown" in log and bool(
        re.search(
            r"All \d+ inference request\(s\) failed; no successful responses were collected", log
        )
    )


def persist_run(directory, data):
    with (directory / "state.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = load_run(directory)
        if current.status not in {"queued", "running"}:
            return current
        run = Run.model_validate(data)
        atomic_json(directory / "heartbeat.json", dict(pid=os.getpid(), at=utcnow()))
        atomic_json(directory / "run.json", run)
        if (directory / "manifest.json").exists():
            publish_observation(directory, run)
        return run


def publish_observation(directory, run):
    """Publish C's protocol without interpreting remote process IDs or Job ownership."""
    clock_path = directory / "engine-clock.json"
    origin = None
    if clock_path.is_file():
        clock = json.loads(clock_path.read_text())
        origin = datetime.fromtimestamp(clock["epoch_ns"] / 1e9, UTC)
    observation = RunnerObservation(
        status=run.status,
        stage=run.stage,
        result=run.result,
        error=run.error,
        timeline_origin_at=origin,
    )
    if run.result is not None:
        atomic_json(directory / "result.json", run.result)
    atomic_json(directory / "observation.json", observation)


def target_readiness(snapshot):
    from urllib.parse import urlsplit

    url = urlsplit(str(snapshot.target.endpoint))
    origin = f"{url.scheme}://{url.netloc}"
    with httpx.Client(timeout=5, trust_env=False) as client:
        if snapshot.target.mode == "local-stub":
            response = client.get(origin + "/health")
            response.raise_for_status()
            if response.json().get("fixture") is not True:
                raise ValueError("local-stub target did not identify itself as a fixture endpoint")
        else:
            probes = []
            profiles = {p.ref: p for p in snapshot.serving_profiles}
            bindings = {b.customer_ref: b for b in snapshot.bindings}
            for customer in snapshot.customers:
                variable = snapshot.target.credential_env.get(customer.id)
                headers = {"Authorization": "Bearer " + os.environ[variable]} if variable else {}
                profile = profiles[bindings[customer.ref].serving_profile_ref]
                if snapshot.target.serving_stack == "direct-vllm":
                    response = client.get(origin + "/v1/models", headers=headers)
                    response.raise_for_status()
                    if response.json().get("fixture") is True:
                        raise ValueError("benchmark target identifies itself as a fixture endpoint")
                    if customer.model_alias not in {item["id"] for item in response.json()["data"]}:
                        raise ValueError(
                            f"endpoint is not ready for model alias {customer.model_alias}"
                        )
                # Gateways need not expose a model catalogue. Probe each routed alias using an
                # unscored, bounded call without benchmark records or quality inputs.
                payload = dict(
                    model=customer.model_alias,
                    messages=[{"role": "user", "content": "Reply OK."}],
                    max_tokens=1,
                    temperature=0.0,
                    stream=False,
                )
                if profile.engine.chat_template_kwargs:
                    payload["chat_template_kwargs"] = profile.engine.chat_template_kwargs
                response = client.post(
                    str(snapshot.target.endpoint), headers=headers, json=payload, timeout=30
                )
                response.raise_for_status()
                body = response.json()
                if body.get("fixture") is True:
                    raise ValueError("benchmark target identifies itself as a fixture endpoint")
                if not body.get("choices"):
                    raise ValueError("benchmark readiness probe returned no completion")
                probes.append(
                    {
                        "customer_ref": customer.ref,
                        "model_alias": customer.model_alias,
                        "status": response.status_code,
                    }
                )
            return probes


def execute(directory: Path):
    """Execute once. Exceptions become durable terminal observations, with partial records."""
    directory = directory.resolve()
    with (directory / "execution.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("attempt is already executing") from None
        run = load_run(directory)
        if run.status != "queued":
            return run
        data = run.model_dump(mode="json")
        data.update(status="running", stage="preparing", started_at=utcnow())
        persist_run(directory, data)
        atomic_json(directory / "heartbeat.json", dict(pid=os.getpid(), at=utcnow()))
        child = None
        interrupted = False
        previous = {}

        def cancel(_signum, _frame):
            (directory / "cancel-requested").touch()

        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, cancel)
        try:
            if (directory / "cancel-requested").exists():
                build_engine_inputs(run.snapshot, directory)
                raise RuntimeError("execution cancelled before engine launch")
            preflight(run.snapshot)
            probes = target_readiness(run.snapshot)
            if probes:
                atomic_json(directory / "readiness.json", probes)
            build_engine_inputs(run.snapshot, directory)
            environment = dict(os.environ, PLATFORM_RUN_DIR=str(directory))
            start = time.monotonic()
            budget = (
                run.snapshot.scenario.warmup.duration_s
                + run.snapshot.scenario.measurement_duration_s
                + run.snapshot.scenario.drain_timeout_s
            )
            with (directory / "engine.log").open("w") as log:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "aiperf",
                        "profile",
                        "--config",
                        str(directory / "aiperf-config.yaml"),
                    ],
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                while child.poll() is None:
                    elapsed = time.monotonic() - start
                    clock_file = directory / "engine-clock.json"
                    stage = "preparing"
                    if clock_file.exists():
                        clock = json.loads(clock_file.read_text())
                        engine_elapsed = (time.perf_counter_ns() - clock["perf_ns"]) / 1e9
                        stage = (
                            "warming_up"
                            if engine_elapsed < run.snapshot.scenario.warmup.duration_s
                            else "measuring"
                            if engine_elapsed < budget - run.snapshot.scenario.drain_timeout_s
                            else "draining"
                        )
                        # The transport enforces the workload deadline. Leave separate time
                        # for native export/shutdown rather than interrupting healthy exports.
                        if engine_elapsed > budget + 60:
                            raise RuntimeError("AIPerf export/shutdown watchdog expired")
                    if (directory / "cancel-requested").exists():
                        interrupted = True
                    if not clock_file.exists() and elapsed > 120:
                        raise RuntimeError("AIPerf startup watchdog expired")
                    data["stage"] = stage
                    if persist_run(directory, data).status != "running":
                        raise RuntimeError("attempt ownership was lost")
                    if interrupted:
                        child.send_signal(signal.SIGINT)
                        try:
                            child.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(child.pid, signal.SIGKILL)
                            child.wait(timeout=5)
                        break
                    time.sleep(0.1)
            data["stage"] = "finalizing"
            persist_run(directory, data)
            scrub_artifacts(directory, run.snapshot)
            cancelled = (directory / "cancel-requested").exists()
            requests, sessions = normalize(run.snapshot, directory, run.id, cancelled=cancelled)
            completed = engine_completed(directory, child.returncode, requests)
            quality = evaluate_quality(run.snapshot, requests, directory)
            from slo_evaluator import evaluate

            artifacts = result_artifacts(directory)
            result = evaluate(
                run.snapshot,
                requests,
                sessions,
                quality=quality,
                artifacts=artifacts,
                invalid_reasons=["execution_cancelled"]
                if cancelled
                else ["engine_failed"]
                if not completed
                else [],
            )
            data.update(
                status="cancelled" if cancelled else "completed" if completed else "failed",
                stage=None,
                finished_at=utcnow(),
                result=result.model_dump(mode="json"),
            )
            if data["status"] == "failed":
                data["error"] = dict(
                    code="engine_failed", message="AIPerf failed; see retained engine.log"
                )
        except Exception as error:
            if child and child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
            data.update(
                status="cancelled" if (directory / "cancel-requested").exists() else "failed",
                stage=None,
                finished_at=utcnow(),
                error=dict(code="execution_failed", message=f"{type(error).__name__}: {error}"),
            )
            if data["status"] == "cancelled":
                data["error"] = None
            if (directory / "engine-context.json").exists() and not data.get("result"):
                try:
                    from slo_evaluator import evaluate

                    requests, sessions = normalize(
                        run.snapshot,
                        directory,
                        run.id,
                        cancelled=(directory / "cancel-requested").exists(),
                    )
                    result = evaluate(
                        run.snapshot,
                        requests,
                        sessions,
                        artifacts=result_artifacts(directory),
                        invalid_reasons=[
                            "execution_cancelled"
                            if data["status"] == "cancelled"
                            else "execution_failed"
                        ],
                    )
                    data["result"] = result.model_dump(mode="json")
                except (ValueError, OSError, KeyError):
                    pass  # Keep raw artifacts when their normalization itself failed.
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        if data.get("error"):
            data["error"]["message"] = redact(
                data["error"]["message"], credential_values(run.snapshot)
            )
        scrub_artifacts(directory, run.snapshot)
        final = persist_run(directory, data)
        write_report(final, directory)
        if final.result:
            data = final.model_dump(mode="json")
            data["result"]["artifacts"] = result_artifacts(directory)
            final = Run.model_validate(data)
            atomic_json(directory / "run.json", final)
        if (directory / "manifest.json").exists():
            publish_observation(directory, final)
        return final


class LocalRunnerAdapter:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def launch(self, snapshot: RunSnapshot, operation_key: str) -> str:
        if not operation_key:
            raise ValueError("operation_key must be nonempty")
        self.root.mkdir(parents=True, exist_ok=True)
        directory = self.root / ("attempt-" + digest(operation_key.encode())[:32])
        with (self.root / "launch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if (directory / "snapshot.json").exists():
                original = RunSnapshot.model_validate_json(
                    (directory / "snapshot.json").read_text()
                )
                if canonical(original) != canonical(snapshot):
                    raise ValueError("operation key already identifies different inputs")
                if not (directory / "run.json").exists():
                    interrupted = Run(
                        id="attempt-" + digest(str(directory).encode())[:24],
                        fixture=original.target.mode == "local-stub",
                        status="failed",
                        created_at=datetime.fromtimestamp(
                            (directory / "snapshot.json").stat().st_mtime, UTC
                        ),
                        finished_at=utcnow(),
                        snapshot=original,
                        error=dict(
                            code="interrupted_launch",
                            message="Attempt initialization was interrupted; no restart attempted",
                        ),
                    )
                    atomic_json(directory / "run.json", interrupted)
                return str(directory)
            preflight(snapshot)
            initialize_attempt(snapshot, directory)
            try:
                with (directory / "supervisor.log").open("w") as log:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "workload_runner.cli",
                            "run",
                            "--attempt",
                            str(directory),
                        ],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                # Child takes state.lock for its first heartbeat; don't replace a
                # heartbeat it has already advanced while this process was spawning.
                with (directory / "state.lock").open("a") as state_lock:
                    fcntl.flock(state_lock, fcntl.LOCK_EX)
                    if load_run(directory).status == "queued":
                        atomic_json(
                            directory / "heartbeat.json", dict(pid=process.pid, at=utcnow())
                        )
            except OSError as error:
                data = load_run(directory).model_dump(mode="json")
                data.update(
                    status="failed",
                    finished_at=utcnow(),
                    error=dict(code="launch_failed", message=type(error).__name__),
                )
                persist_run(directory, data)
                raise
        return str(directory)

    def _directory(self, handle):
        directory = Path(handle).resolve()
        if directory.parent != self.root or not (directory / "run.json").is_file():
            raise ValueError("unknown runner handle")
        return directory

    def stop(self, handle: str) -> None:
        directory = self._directory(handle)
        if load_run(directory).status in {"queued", "running"}:
            (directory / "cancel-requested").touch()

    def observe(self, handle: str) -> RunnerObservation:
        with (self.root / "launch.lock").open("a") as launch_lock:
            fcntl.flock(launch_lock, fcntl.LOCK_EX)
            directory = self._directory(handle)
            with (directory / "state.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                return self._observe_locked(directory)

    def _observe_locked(self, directory):
        run = load_run(directory)
        if run.status in {"queued", "running"}:
            heartbeat_path = directory / "heartbeat.json"
            heartbeat = json.loads(heartbeat_path.read_text()) if heartbeat_path.exists() else None
            alive = False
            if heartbeat:
                try:
                    os.kill(heartbeat["pid"], 0)
                    age = (
                        datetime.now(UTC) - datetime.fromisoformat(heartbeat["at"])
                    ).total_seconds()
                    alive = age < 30
                except ProcessLookupError:
                    pass
            if not alive:
                data = run.model_dump(mode="json")
                data.update(
                    status="failed",
                    stage=None,
                    finished_at=utcnow(),
                    error=dict(
                        code="lost_attempt",
                        message="Runner process or heartbeat was lost; no restart attempted",
                    ),
                )
                run = Run.model_validate(data)
                atomic_json(directory / "run.json", run)
        return RunnerObservation(
            status=run.status, stage=run.stage, result=run.result, error=run.error
        )


def reassess(directory):
    from platform_contracts import RequestRecord, SessionRecord
    from slo_evaluator import evaluate

    run = load_run(directory)
    preps = run.snapshot.preparation
    for prep in preps:
        artifact_path(prep.artifact.uri, prep.artifact.sha256)
    if run.result is None:
        raise ValueError("reassessment requires a terminal result with checksummed records")
    for name in ("requests", "sessions"):
        artifact = run.result.artifacts.get(name)
        if artifact is None or artifact.sha256 is None:
            raise ValueError("original result has no checksummed " + name)
        expected = directory / (name + ".jsonl")
        if artifact_path(artifact.uri, artifact.sha256).resolve() != expected.resolve():
            raise ValueError("assessment artifact location disagrees with original result")
    schedule = directory / "admission-schedule.jsonl"
    if (
        run.snapshot.admission_schedule
        and digest(schedule.read_bytes()) != run.snapshot.admission_schedule.sha256
    ):
        raise ValueError("saved admission schedule checksum differs from frozen inputs")
    requests = list(read_jsonl(directory / "requests.jsonl", RequestRecord))
    sessions = list(read_jsonl(directory / "sessions.jsonl", SessionRecord))
    output = directory / "assessments" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    quality = evaluate_quality(run.snapshot, requests, output, source_directory=directory)
    result = evaluate(
        run.snapshot,
        requests,
        sessions,
        quality=quality,
        invalid_reasons=run.result.invalid_reasons
        if run.result
        else ("execution_did_not_complete",),
    )
    atomic_json(output / "result.json", result)
    atomic_json(
        output / "lineage.json",
        dict(
            original_run_sha256=digest((directory / "run.json").read_bytes()),
            request_sha256=digest((directory / "requests.jsonl").read_bytes()),
            evaluator_version="0.1.0",
        ),
    )
    return output
