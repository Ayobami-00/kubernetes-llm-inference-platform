"""Verify immutable child artifacts, analyse on CPU and register the parent report."""

import hashlib
import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx
from platform_contracts import PreparedEntry, RequestRecord, Run
from workload_runner.artifacts import read_jsonl
from workload_runner.experiments.comparison import compare_pair, public_comparison

from platform_management.db.models import ArtifactCheck, RunRow
from platform_management.db.session import session
from platform_management.integrations.artifacts import LocalArtifactAccess
from platform_management.integrations.job_runner import frozen_file
from platform_management.services.common import digest, now
from platform_management.services.imports import inspect_artifacts
from platform_management.settings import get_settings


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def diagnostic_metrics(run, context, readiness):
    """Thanos deduplication is explicit; Prometheus increases are estimates, not client counts."""
    origin = context.get("timeline_origin_at")
    if not origin:
        return {"status": "unavailable", "reason": "No recorded client timeline origin"}
    end = datetime.fromisoformat(origin).timestamp() + 360
    pods = readiness.get("pods", [])
    if len(pods) != 1:
        return {"status": "unavailable", "reason": "Expected one pinned serving Pod"}
    selector = '{namespace="llm-d",service="writer-assist-a10",pod="' + pods[0]["name"] + '"}'
    expressions = {
        name: f"sum(increase({metric}{selector}[300s] @ {end}))"
        for name, metric in {
            "draft_events": "vllm:spec_decode_num_drafts_total",
            "proposed_tokens": "vllm:spec_decode_num_draft_tokens_total",
            "accepted_tokens": "vllm:spec_decode_num_accepted_tokens_total",
        }.items()
    }
    gpu = '{namespace="gpu-system",UUID="GPU-aa3bb9d3-11f4-f795-1d71-127f61539a1d"}'
    expressions["gpu_utilization_mean_percent"] = (
        f"avg(avg_over_time(DCGM_FI_DEV_GPU_UTIL{gpu}[300s] @ {end}))"
    )
    expressions["gpu_framebuffer_max_mib"] = (
        f"max(max_over_time(DCGM_FI_DEV_FB_USED{gpu}[300s] @ {end}))"
    )
    expressions["waiting_requests_mean"] = (
        f"avg(avg_over_time(vllm:num_requests_waiting{selector}[300s] @ {end}))"
    )
    expressions["generated_tokens"] = (
        f"sum(increase(vllm:generation_tokens_total{selector}[300s] @ {end}))"
    )
    for name, metric in {
        "server_completion_p95_seconds": "vllm:e2e_request_latency_seconds_bucket",
        "server_queue_p95_seconds": "vllm:request_queue_time_seconds_bucket",
        "server_ttft_p95_seconds": "vllm:time_to_first_token_seconds_bucket",
    }.items():
        expressions[name] = (
            f"histogram_quantile(0.95,sum by(le)(increase({metric}{selector}[300s] @ {end})))"
        )
    result = {
        "status": "observed",
        "scope": (
            "Scored 300-second interval; deduplicated Prometheus reset-aware increase "
            "estimates with scrape-edge extrapolation, not exact per-request counts."
        ),
        "queries": expressions,
        "values": {},
    }
    with httpx.Client(timeout=10, trust_env=False) as client:

        def query(item):
            name, expression = item
            try:
                response = client.get(
                    "http://klip-thanos-query.observability.svc.cluster.local:9090/api/v1/query",
                    params={"query": expression, "dedup": "true"},
                )
                response.raise_for_status()
                rows = response.json()["data"]["result"]
                value = float(rows[0]["value"][1]) if len(rows) == 1 else None
                return name, value if value is not None and math.isfinite(value) else None
            except (httpx.HTTPError, KeyError, ValueError):
                return name, None

        with ThreadPoolExecutor(max_workers=4) as executor:
            result["values"] = dict(executor.map(query, expressions.items()))
    values = result["values"]
    observed = sum(value is not None for value in values.values())
    result["status"] = (
        "observed" if observed == len(values) else "partial" if observed else "unavailable"
    )
    accepted, proposed, drafts = (
        values[k] for k in ("accepted_tokens", "proposed_tokens", "draft_events")
    )
    values["acceptance_fraction"] = (
        accepted / proposed if accepted is not None and proposed else None
    )
    values["accepted_tokens_per_draft"] = (
        accepted / drafts if accepted is not None and drafts else None
    )
    return result


def register(files):
    checks = inspect_artifacts(files)
    if any(check["status"] != "verified" for check in checks):
        raise ValueError("A comparison artifact could not be verified")
    by_uri = {check["uri"]: check for check in checks}
    with session() as db, db.begin():
        for check in checks:
            db.merge(ArtifactCheck(**check))
    return {name: {**item, "id": by_uri[item["uri"]]["id"]} for name, item in files.items()}


def describe(path):
    return {"uri": path.resolve().as_uri(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def terminal_artifacts(data, root):
    """Even a startup cancellation has an immutable, downloadable execution record."""
    frozen_file(root / "definition.json", json_bytes(data["definition"]))
    frozen_file(root / "terminal-state.json", json_bytes(data))
    state = json.loads((root / "terminal-state.json").read_text())
    lines = [
        "# A10 paired experiment: " + state["status"],
        "",
        "Experiment: `" + state["id"] + "`",
        "",
        "No new paired performance claim is made by this terminal summary. "
        "Completed and partial child assessments remain separate and immutable.",
        "",
        "The retained VM has no experiment-owned lease or shutdown action.",
        "",
        "```json",
        json.dumps(state, indent=2, sort_keys=True),
        "```",
        "",
    ]
    frozen_file(root / "terminal-report.md", "\n".join(lines).encode())
    files = {
        "definition": describe(root / "definition.json"),
        "execution": describe(root / "terminal-state.json"),
    }
    if data["status"] != "completed":
        files["report"] = describe(root / "terminal-report.md")
    return register(files)


def render_report(data, comparison, runs):
    text = data["definition"]["report_template"]
    lines = [
        text,
        "",
        "## Recorded execution",
        "",
        f"Experiment: `{data['id']}`",
        f"Conclusion: **{comparison['conclusion']}**",
        "Total wall time through report generation: "
        f"{comparison['total_wall_seconds']:.1f} seconds.",
        "",
        "| Measurement | Baseline | N-gram |",
        "|---|---:|---:|",
    ]
    left, right = comparison["baseline"], comparison["ngram"]
    for label, getter in (
        ("Child run", lambda v: v["run_id"]),
        ("Execution", lambda v: v["execution_status"]),
        ("Measurement validity", lambda v: v["measurement_validity"]),
        ("Eligible scored requests", lambda v: v["quality"]["coverage"]["eligible"]),
        ("Successful latency samples", lambda v: v["completion_latency"]["samples"]),
        ("Completion p50 ms", lambda v: v["completion_latency"]["p50_ms"]),
        ("Completion p95 ms", lambda v: v["completion_latency"]["p95_ms"]),
        ("Completion p99 ms", lambda v: v["completion_latency"]["p99_ms"]),
        ("SLO verdict", lambda v: v["slo_verdict"]),
        ("Full-cohort GLEU", lambda v: v["quality"]["gleu"]),
        ("Quality evaluated", lambda v: v["quality"]["coverage"]["evaluated"]),
        ("Quality failed", lambda v: v["quality"]["coverage"]["failed"]),
        (
            "Quality missing/unscorable",
            lambda v: v["quality"]["coverage"]["missing"] + v["quality"]["coverage"]["unscorable"],
        ),
        ("Missed admissions", lambda v: v["delivery"]["missed_due_admissions"]),
        ("Admissions late above 100 ms", lambda v: v["delivery"]["late_above_100ms"]),
    ):
        lines.append(f"| {label} | {getter(left)} | {getter(right)} |")
    lines.extend(
        [
            "",
            "p95 reduction = 100 × (1 − n-gram p95 / baseline p95). "
            "A negative value is a regression. Unavailable values remain unknown.",
            "",
            f"Observed p95 reduction: {comparison['p95_reduction_percent']}%.",
            "",
            "### Frozen controls and data",
            "",
            "```json",
            json.dumps(
                {k: v for k, v in data["definition"].items() if k != "report_template"},
                indent=2,
                sort_keys=True,
            ),
            "```",
            "",
            "### Exact stage timeline",
            "",
            "```json",
            json.dumps(comparison["stages"], indent=2),
            "```",
            "",
            "### SLO, quality, uncertainty and diagnostic evidence",
            "",
            "```json",
            json.dumps(public_comparison(comparison), indent=2, sort_keys=True),
            "```",
            "",
            "### Child artifact index",
            "",
        ]
    )
    for run in runs:
        lines.append(f"Child `{run.id}`:")
        lines.append("")
        for name, artifact in run.result.artifacts.items():
            lines.append(f"- {name}: `{artifact.uri}` SHA-256 `{artifact.sha256}`")
        lines.append("")
    return "\n".join(lines) + "\n"


def build_comparison(data, root):
    access = LocalArtifactAccess(get_settings().artifacts_root)
    with session() as db:
        children = [db.get(RunRow, data["children"][variant]) for variant in ("baseline", "ngram")]
        runs = [Run.model_validate(child.payload) for child in children]
        contexts = [dict(child.context) for child in children]
    identity = digest(
        {
            "definition": data["definition_sha256"],
            "runs": [run.model_dump(mode="json") for run in runs],
        }
    )
    saved = root / "comparison.json"
    analysis = root / "analysis-state.json"
    if analysis.exists():
        state = json.loads(analysis.read_text())
        comparison = state["comparison"]
        if comparison["analysis_inputs_sha256"] != identity:
            raise ValueError("Historical comparison inputs changed")
    else:
        records, schedules = [], []
        for run in runs:
            if not run.result:
                raise ValueError("Missing child result")
            checks = inspect_artifacts(run.result.model_dump(mode="json"))
            if any(check["status"] != "verified" for check in checks):
                raise ValueError("Child result artifacts changed")
            records.append(
                list(
                    read_jsonl(
                        access.path(str(run.result.artifacts["requests"].uri)), RequestRecord
                    )
                )
            )
            schedules.append(
                list(read_jsonl(access.path(str(run.result.artifacts["schedule"].uri))))
            )
        prep = data["definition"]["preparation"]
        if not access.verify(prep["artifact"]["uri"], prep["artifact"]["sha256"]):
            raise ValueError("Prepared input/reference artifact changed before comparison")
        entries = {e.id: e for e in read_jsonl(access.path(prep["artifact"]["uri"]), PreparedEntry)}
        artifact = data["definition"]["analysis_tokenizer"]
        if not access.verify(artifact["uri"], artifact["sha256"]):
            raise ValueError("Pinned analysis tokenizer artifact changed")
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_file(str(access.path(artifact["uri"])))
        comparison = compare_pair(
            runs,
            records,
            schedules,
            entries,
            lambda text: tokenizer.encode(text, add_special_tokens=False).ids,
        )
        comparison["server_diagnostics"] = {
            variant: diagnostic_metrics(
                run, context, json.loads((root / variant / "readiness.json").read_text())
            )
            for variant, run, context in zip(("baseline", "ngram"), runs, contexts, strict=True)
        }
        completed = now()
        stages = json.loads(json.dumps(data["stages"]))
        for details in stages.values():
            ended = details.get("finished_at") or completed.isoformat()
            details["observed_wall_seconds"] = (
                datetime.fromisoformat(ended) - datetime.fromisoformat(details["started_at"])
            ).total_seconds()
        comparison.update(
            analysis_inputs_sha256=identity,
            report_generated_at=completed.isoformat(),
            total_wall_seconds=(
                completed - datetime.fromisoformat(data["created_at"])
            ).total_seconds(),
            stages=stages,
            timing_convention=(
                "Orchestration uses durable UTC wall-clock boundaries across worker recovery; "
                "client durations use AIPerf monotonic timing. Polling gaps and terminal "
                "registration are visible in the parent execution record."
            ),
        )
        state = {"comparison": comparison, "report_context": data}
        # Freeze calculation/clock/diagnostics before materializing output files.
        # Recovery reproduces exact bytes even if the worker died between writes.
        frozen_file(analysis, json_bytes(state))
    frozen_file(root / "disagreements.json", json_bytes(comparison.pop("private_disagreements")))
    frozen_file(root / "token-overlap.json", json_bytes(comparison.pop("per_request_overlap")))
    frozen_file(
        root / "report.md", render_report(state["report_context"], comparison, runs).encode()
    )
    frozen_file(saved, json_bytes(comparison))
    files = {
        name: describe(root / filename)
        for name, filename in {
            "comparison": "comparison.json",
            "report": "report.md",
            "disagreements": "disagreements.json",
            "overlap": "token-overlap.json",
            "definition": "definition.json",
        }.items()
    }
    return public_comparison(comparison), register(files)
