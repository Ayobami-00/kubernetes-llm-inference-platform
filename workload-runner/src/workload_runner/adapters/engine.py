"""Build native AIPerf inputs and normalize its observed HTTP records."""

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml
from platform_contracts import Admission, PreparedEntry, RequestRecord, SessionRecord

from workload_runner.artifacts import artifact_path, atomic_json, read_jsonl, write_jsonl


def build_engine_inputs(snapshot, directory: Path):
    admissions = list(
        read_jsonl(
            artifact_path(snapshot.admission_schedule.uri, snapshot.admission_schedule.sha256),
            Admission,
        )
    )
    write_jsonl(directory / "admission-schedule.jsonl", admissions)
    prepared = {
        p.customer_ref: {
            e.id: e
            for e in read_jsonl(artifact_path(p.artifact.uri, p.artifact.sha256), PreparedEntry)
        }
        for p in snapshot.preparation
    }
    if len({a.id for a in admissions}) != len(admissions):
        raise ValueError("duplicate admission identity")
    selected = set()
    for admission in admissions:
        source = snapshot.derivation.get(admission.customer_ref)
        if source not in prepared or admission.entry_id not in prepared[source]:
            raise ValueError("schedule references missing customer or prepared entry")
        entry = prepared[source][admission.entry_id]
        if entry.warmup != admission.warmup:
            raise ValueError("schedule cohort disagrees with prepared entry")
        key = (admission.customer_ref, entry.source_id)
        if key in selected:
            raise ValueError("schedule samples a source input more than once")
        selected.add(key)
        offset = 0
        phases = [("warmup", snapshot.scenario.warmup.duration_s)] + [
            (p.id, p.duration_s) for p in snapshot.scenario.phases
        ]
        expected = None
        for phase, duration in phases:
            if offset <= admission.scheduled_offset_ms < offset + duration * 1000:
                expected = phase
            offset += duration * 1000
        if expected != admission.phase or admission.warmup != (expected == "warmup"):
            raise ValueError("admission is outside its declared phase")
    customers = {c.ref: c for c in snapshot.customers}
    bindings = {b.customer_ref: b for b in snapshot.bindings}
    rows, sessions = [], {}
    for admission in admissions:
        customer = customers[admission.customer_ref]
        source_ref = snapshot.derivation[customer.ref]
        entry = prepared[source_ref][admission.entry_id]
        turns = []
        for index, turn in enumerate(entry.turns):
            payload = dict(turn.controls)
            payload.update(
                model=customer.model_alias,
                messages=list(turn.messages),
                stream=customer.generation.streaming,
            )
            if turn.tools:
                payload["tools"] = list(turn.tools)
            headers = {
                "x-platform-task": customer.workload.task,
                "x-platform-run-id": directory.name,
                "x-platform-request-id": f"{admission.id}-{index}",
                "x-platform-deployment": bindings[customer.ref].deployment_id,
                "x-platform-preset": snapshot.target.stub_preset,
            }
            if snapshot.target.mode == "benchmark":
                headers = {}
            turns.append(dict(payload=payload, headers=headers, delay_ms=turn.delay_ms))
        rows.append(
            dict(
                session_id=admission.id,
                timestamp=admission.scheduled_offset_ms,
                timestamp_ms=admission.scheduled_offset_ms,
                context_mode=entry.context_mode,
                turns=turns,
            )
        )
        sessions[admission.id] = dict(
            admission=admission.model_dump(mode="json"),
            customer_ref=customer.ref,
            entry_id=entry.id,
            expected_turns=len(turns),
            delays_ms=[t.delay_ms for t in entry.turns],
            headers=[turn["headers"] for turn in turns],
        )
    write_jsonl(directory / "engine-inputs.jsonl", rows)
    end_ms = (snapshot.scenario.warmup.duration_s + snapshot.scenario.measurement_duration_s) * 1000
    context = dict(
        sessions=sessions,
        admission_end_ms=end_ms,
        deadline_ms=end_ms + snapshot.scenario.drain_timeout_s * 1000,
        customers={
            ref: dict(
                max_inflight=c.limits.max_inflight_requests,
                timeout_ms=c.limits.request_timeout_ms,
                credential_env=snapshot.target.credential_env.get(c.id),
            )
            for ref, c in customers.items()
        },
    )
    atomic_json(directory / "engine-context.json", context)
    config = {
        "schemaVersion": "2.0",
        "randomSeed": snapshot.scenario.seed,
        "benchmark": {
            "model": "platform",
            "endpoint": {
                "url": str(snapshot.target.endpoint),
                "type": "chat",
                "streaming": True,
                "timeout": max(c.limits.request_timeout_ms for c in customers.values()) / 1000,
                "waitForModelTimeout": 0,
            },
            "dataset": {
                "type": "file",
                "path": str((directory / "engine-inputs.jsonl").resolve()),
                "format": "raw_payload",
            },
            "tokenizer": {"name": "builtin"},
            "phases": {
                "type": "fixed_schedule",
                "autoOffset": False,
                # Native concurrency bounds whole sessions, including think time.
                # HTTP inflight limits are enforced separately by the transport.
                "concurrency": max(
                    len(admissions), sum(c.limits.max_inflight_requests for c in customers.values())
                ),
            },
            "gpuTelemetry": {"enabled": False},
            "runtime": {"ui": "none", "workers": 1, "recordProcessors": 1},
            "artifacts": {
                "dir": str((directory / "aiperf").resolve()),
                "raw": True,
                "summary": ["json"],
                "records": ["jsonl"],
            },
        },
    }
    (directory / "aiperf-config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    return context


def response_parts(record):
    """Return content, tools, first meaningful perf timestamp, finish and usage.

    AIPerf already frames SSE; role-only and usage/control messages aren't first output.
    """
    text, tools, first, finish, usage = "", {}, None, None, None
    for response in record.get("responses", []):
        data = response.get("data", response.get("text"))
        if data is None and "packets" in response:
            data = "\n".join(p["value"] for p in response["packets"] if p["name"] == "data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (ValueError, TypeError):
                continue
        if not isinstance(data, dict):
            continue
        if data.get("usage"):
            usage = data["usage"]
        for choice in data.get("choices", []):
            message = choice.get("delta", choice.get("message", {}))
            content = message.get("content") or ""
            calls = message.get("tool_calls") or []
            meaningful_calls = any(
                c.get("function", {}).get("name") or c.get("function", {}).get("arguments")
                for c in calls
            )
            if (content or meaningful_calls) and first is None:
                first = response.get("perf_ns")
            text += content
            for call in calls:
                index = call.get("index", 0)
                target = tools.setdefault(
                    index, {"type": "function", "function": {"name": "", "arguments": ""}}
                )
                if call.get("id"):
                    target["id"] = call["id"]
                function = call.get("function") or {}
                for field in ("name", "arguments"):
                    target["function"][field] += function.get(field) or ""
            finish = choice.get("finish_reason") or finish
    return text, [tools[key] for key in sorted(tools)], first, finish, usage


def normalize(snapshot, directory: Path, run_id: str, *, cancelled=False):
    context = json.loads((directory / "engine-context.json").read_text())
    clock_path = directory / "engine-clock.json"
    clock = json.loads(clock_path.read_text()) if clock_path.exists() else None
    origin = clock["perf_ns"] if clock else None
    events_path = directory / "aiperf" / "transport-events.jsonl"
    events = list(read_jsonl(events_path)) if events_path.exists() else []
    dispatch = {
        (e["conversation_id"], e["turn_index"]): e for e in events if e["kind"] == "dispatch"
    }
    observed = {(e["conversation_id"], e["turn_index"]): e for e in events if e["kind"] == "record"}
    cutoffs = {(e["conversation_id"], e["turn_index"]) for e in events if e["kind"] == "cutoff"}
    requests, sessions = [], []
    warm_ms = snapshot.scenario.warmup.duration_s * 1000
    end_ms = warm_ms + snapshot.scenario.measurement_duration_s * 1000
    for session_id, plan in context["sessions"].items():
        admission = plan["admission"]
        owned = []
        for index in range(plan["expected_turns"]):
            key = (session_id, index)
            sent = dispatch.get(key)
            item = observed.get(key)
            if not sent and index > 0:
                break  # A response-dependent turn was never admitted; not a missed arrival.
            scheduled = (
                admission["scheduled_offset_ms"]
                if index == 0
                else (
                    owned[-1].completed_offset_ms + plan["delays_ms"][index]
                    if owned[-1].completed_offset_ms is not None
                    else None
                )
            )
            record = item["record"] if item else {}
            text, tools, first, finish, usage = response_parts(record)
            start = record.get("start_perf_ns", sent["perf_ns"] if sent else None)
            offset = (start - origin) / 1e6 if sent and origin and start is not None else None
            stop = record.get("end_perf_ns")
            status = record.get("status")
            error = record.get("error")
            error_type = str(error.get("type", "") if isinstance(error, dict) else error or "")
            outcome = (
                "not_sent"
                if not sent
                else "cancelled"
                if not item
                else "rejected"
                if status == 429
                else "timed_out"
                if "timeout" in error_type.lower()
                else "cancelled"
                if record.get("cancellation_perf_ns") is not None
                else "failed"
                if error or not finish or finish == "length" or (status and status >= 400)
                else "completed"
            )
            if cancelled and not item and sent:
                outcome = "cancelled"
            if record.get("cancellation_perf_ns") is not None and not cancelled:
                cutoffs.add(key)
            phase = (
                "warmup"
                if admission["warmup"]
                else "drain"
                if offset is not None and offset >= end_ms
                else "measurement"
            )
            duration = (stop - start) / 1e6 if stop is not None and start is not None else None
            ttft = (first - start) / 1e6 if first is not None and start is not None else None
            if item and not item["payload"].get("stream"):
                ttft = None
            output_tokens = usage.get("completion_tokens") if usage else None
            token_source = (
                ("emulated" if snapshot.target.mode == "local-stub" else "server")
                if usage
                else "unavailable"
            )
            owned.append(
                RequestRecord(
                    run_id=run_id,
                    request_id=f"{session_id}-{index}",
                    customer_ref=plan["customer_ref"],
                    source_entry_id=plan["entry_id"],
                    session_id=session_id,
                    turn_index=index,
                    warmup_cohort=admission["warmup"],
                    phase=phase,
                    scheduled_offset_ms=scheduled,
                    actual_send_offset_ms=offset,
                    completed_offset_ms=(stop - origin) / 1e6 if stop and origin else None,
                    latency_ms=duration,
                    ttft_ms=ttft,
                    tpot_ms=(
                        (duration - ttft) / (output_tokens - 1)
                        if duration is not None
                        and ttft is not None
                        and output_tokens
                        and output_tokens > 1
                        else None
                    ),
                    timing_source="aiperf_monotonic" if item else "unavailable",
                    sent_at=(
                        datetime.fromtimestamp(
                            (clock["epoch_ns"] + start - origin) / 1e9, UTC
                        ).isoformat()
                        if sent and clock
                        else None
                    ),
                    outcome=outcome,
                    eligible_for_slo=bool(
                        not admission["warmup"]
                        and offset is not None
                        and warm_ms <= offset < end_ms
                    ),
                    http_status=status,
                    finish_reason=finish,
                    complete_output=outcome == "completed",
                    input_tokens=usage.get("prompt_tokens") if usage else None,
                    output_tokens=output_tokens,
                    cached_input_tokens=(usage.get("prompt_tokens_details") or {}).get(
                        "cached_tokens"
                    )
                    if usage
                    else None,
                    token_count_source=token_source,
                    output_text=text or None,
                    tool_calls=tools,
                    error=error_type
                    or ("incomplete_output" if outcome == "failed" and not finish else None),
                )
            )
        requests.extend(owned)
        started = next(
            (r.actual_send_offset_ms for r in owned if r.actual_send_offset_ms is not None), None
        )
        ending = next(
            (r.completed_offset_ms for r in reversed(owned) if r.completed_offset_ms is not None),
            None,
        )
        outcome = (
            "not_started"
            if started is None
            else "completed"
            if len(owned) == plan["expected_turns"] and all(r.outcome == "completed" for r in owned)
            else "cancelled"
            if cancelled
            else "cutoff"
            if any((session_id, i) in cutoffs for i in range(plan["expected_turns"]))
            else "failed"
        )
        sessions.append(
            SessionRecord(
                id=session_id,
                customer_ref=plan["customer_ref"],
                source_entry_id=plan["entry_id"],
                warmup_cohort=admission["warmup"],
                scheduled_offset_ms=admission["scheduled_offset_ms"],
                started_offset_ms=started,
                ended_offset_ms=ending,
                expected_turns=plan["expected_turns"],
                request_ids=[r.request_id for r in owned if r.outcome != "not_sent"],
                outcome=outcome,
                reason=None if outcome == "completed" else outcome,
            )
        )
    write_jsonl(directory / "requests.jsonl", requests)
    write_jsonl(directory / "sessions.jsonl", sessions)
    write_jsonl(
        directory / "responses.jsonl",
        [
            dict(
                request_id=r.request_id,
                output_text=r.output_text,
                tool_calls=r.tool_calls,
                finish_reason=r.finish_reason,
                complete_output=r.complete_output,
            )
            for r in requests
        ],
    )
    return requests, sessions
