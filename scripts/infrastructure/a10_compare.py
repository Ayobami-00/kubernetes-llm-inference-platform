"""Compare two immutable WriterAssist runs; never alter either assessment or target."""

import argparse
import hashlib
import json
from pathlib import Path

from platform_contracts import Run


def artifact(directory, name):
    evidence = json.loads((directory / "evidence.json").read_text())
    item = evidence["artifacts"][name]
    path = Path(item["path"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
        raise ValueError("Downloaded comparison artifact checksum differs")
    return path


def records(directory, name):
    return [json.loads(line) for line in artifact(directory, name).read_text().splitlines()]


def common_quality(directory, scored, common):
    from workload_runner.reporting.quality import corpus_gleu

    evidence = json.loads((directory / "evidence.json").read_text())
    transport = next(
        name
        for name, item in evidence["artifacts"].items()
        if item["uri"].endswith("/aiperf/transport-events.jsonl")
    )
    payloads = {
        r["conversation_id"]: r["payload"]
        for r in records(directory, transport)
        if r["kind"] == "record"
    }
    quality = next(iter(json.loads(artifact(directory, "quality").read_text()).values()))
    refs = {r["request_id"]: r["reference"]["corrections"] for r in quality["review_bundle"]}
    examples = [
        (
            payloads[scored[key]["session_id"]]["messages"][-1]["content"],
            scored[key]["output_text"],
            refs[scored[key]["request_id"]],
        )
        for key in sorted(common)
    ]
    return corpus_gleu(examples)


def compare(left, right):
    runs = [
        Run.model_validate(json.loads((p / "management-run.json").read_text())["run"])
        for p in (left, right)
    ]
    if any(
        r.fixture or r.status != "completed" or not r.result or r.result.validity != "valid"
        for r in runs
    ):
        raise ValueError("Only completed measured runs with valid delivery can be compared")
    if any(
        len(r.snapshot.customers) != 1
        or r.snapshot.derivation.get(r.snapshot.customers[0].ref) != "writer-assist@1"
        for r in runs
    ):
        raise ValueError("Comparison is limited to WriterAssist")
    profiles = [r.snapshot.serving_profiles[0].model_dump(mode="json") for r in runs]
    if (
        profiles[0]["engine"]["speculative_decoding"] is not None
        or (profiles[1]["engine"]["speculative_decoding"] or {}).get("method") != "ngram"
    ):
        raise ValueError("Expected baseline followed by n-gram")
    for p in profiles:
        p.pop("revision")
        p.pop("notes", None)
        p["engine"].pop("speculative_decoding")
    if profiles[0] != profiles[1]:
        raise ValueError("Serving settings differ beyond speculation")
    snapshots = [r.snapshot for r in runs]
    if snapshots[0].versions != snapshots[1].versions:
        raise ValueError("Execution versions differ")
    for field in ("generation", "limits", "slo", "traffic_allowance"):
        if getattr(snapshots[0].customers[0], field) != getattr(snapshots[1].customers[0], field):
            raise ValueError("Frozen customer settings differ")
    for field in ("seed", "warmup", "phases", "drain_timeout_s"):
        a, b = (getattr(s.scenario, field) for s in snapshots)
        if field in {"warmup", "phases"}:
            # Customer/dataset references include each immutable plan identity.
            if field == "warmup":
                a, b = a.duration_s, b.duration_s
            else:
                a, b = (
                    [
                        {
                            "id": p.id,
                            "duration_s": p.duration_s,
                            "arrival": next(iter(p.arrivals.values())).model_dump(),
                        }
                        for p in value
                    ]
                    for value in (a, b)
                )
        if a != b:
            raise ValueError("Frozen traffic or timeline differs")
    if snapshots[0].delivery_policy != snapshots[1].delivery_policy:
        raise ValueError("Delivery policy differs")
    if snapshots[0].preparation[0].artifact.sha256 != snapshots[1].preparation[0].artifact.sha256:
        raise ValueError("Prepared corpus differs")
    schedules = [
        [
            {k: v for k, v in row.items() if k not in {"id", "customer_ref"}}
            for row in records(p, "schedule")
        ]
        for p in (left, right)
    ]
    if schedules[0] != schedules[1]:
        raise ValueError("Materialized arrivals or selected input records differ")
    if snapshots[0].target != snapshots[1].target:
        # The credential map keys are derived customer IDs; compare values separately.
        targets = [s.target.model_dump(mode="json") for s in snapshots]
        for t in targets:
            t["credential_env"] = sorted(t["credential_env"].values())
        if targets[0] != targets[1]:
            raise ValueError("Inference target differs")
    nodes = [[n for n in s.inventory.nodes if n.provider == "lambda"] for s in snapshots]
    if any(len(v) != 1 for v in nodes) or nodes[0][0].kubernetes.uid != nodes[1][0].kubernetes.uid:
        raise ValueError("GPU node identity differs")
    requests = [records(p, "requests") for p in (left, right)]
    scored = [{r["source_entry_id"]: r for r in rows if r["eligible_for_slo"]} for rows in requests]
    if set(scored[0]) != set(scored[1]):
        raise ValueError("Scored cohort differs")
    common = [
        key for key in scored[0] if all(rows[key]["outcome"] == "completed" for rows in scored)
    ]
    identical = sum(
        scored[0][key]["output_text"] == scored[1][key]["output_text"] for key in common
    )
    summaries = []
    for run, rows, directory in zip(runs, requests, (left, right), strict=True):
        assessment = run.result.slo_assessments[0]
        lag = [
            max(0, r["actual_send_offset_ms"] - r["scheduled_offset_ms"])
            for r in rows
            if r["actual_send_offset_ms"] is not None
        ]
        summaries.append(
            {
                "run_id": run.id,
                "validity": run.result.validity,
                "slo_verdict": run.result.slo_verdict,
                "requests": run.result.requests.model_dump(),
                "completion_latency_ms": run.result.measurements[0]
                .latencies["completion_latency_ms"]
                .model_dump(),
                "slo_windows": [w.model_dump(mode="json") for w in assessment.windows],
                "good": sum(w.good_requests for w in assessment.windows),
                "eligible": sum(w.eligible_requests for w in assessment.windows),
                "quality": run.result.quality[0].model_dump(mode="json"),
                "quality_coverage": next(
                    iter(json.loads(artifact(directory, "quality").read_text()).values())
                )["coverage"],
                "max_scheduling_lag_ms": max(lag),
                "late_above_100ms": sum(x > 100 for x in lag),
            }
        )
    baseline, ngram = summaries
    p95_ratio = (
        ngram["completion_latency_ms"]["p95_ms"] / baseline["completion_latency_ms"]["p95_ms"]
    )
    return {
        "scope": (
            "Measured private single-A10 WriterAssist comparison; "
            "exploratory, not a capacity promise"
        ),
        "same_materialized_arrivals_and_inputs": True,
        "same_serving_settings_except_ngram": True,
        "same_customer_targets_and_delivery_policy": True,
        "common_successful_outputs": len(common),
        "identical_common_outputs": identical,
        "common_cohort_gleu": {
            "baseline": common_quality(left, scored[0], common),
            "ngram": common_quality(right, scored[1], common),
            "eligible_successful_in_both": len(common),
            "reference_sampling": "Same sorted source-ID order, seed 0, 500 reference iterations",
        },
        "p95_ratio_ngram_over_baseline": p95_ratio,
        "baseline": baseline,
        "ngram": ngram,
        "limitations": [
            "One non-randomized pair; repeat runs before generalizing speedup.",
            "Latency quantiles include successful requests; "
            "SLO windows include all eligible failures.",
            "GLEU scores use each run's successful output coverage; "
            "compare coverage before interpreting differences.",
            "Gateway upstream resets, if present, are retained. No hidden retries.",
        ],
    }


def report(value):
    lines = [
        "# A10 WriterAssist: baseline and n-gram",
        "",
        value["scope"],
        "",
        "Same pinned model/image/tokenizer, host, customer targets, corpus, "
        "seeded admissions and selected inputs.",
        "",
        "| Metric | Baseline | N-gram |",
        "|---|---:|---:|",
    ]
    a, b = value["baseline"], value["ngram"]
    for label, key in [
        ("Run", "run_id"),
        ("Delivery validity", "validity"),
        ("SLO verdict", "slo_verdict"),
    ]:
        lines.append(f"| {label} | {a[key]} | {b[key]} |")
    for quantile in ["p50_ms", "p95_ms", "p99_ms"]:
        lines.append(
            f"| Completion {quantile} | {a['completion_latency_ms'][quantile]:.2f} | "
            f"{b['completion_latency_ms'][quantile]:.2f} |"
        )
    lines.extend(
        [
            f"| Within 1 second / eligible | {a['good']}/{a['eligible']} | "
            f"{b['good']}/{b['eligible']} |",
            f"| Scored failed responses | {a['quality_coverage']['failed']} | "
            f"{b['quality_coverage']['failed']} |",
            f"| JFLEG GLEU | {a['quality']['scores']['gleu']:.6f} | "
            f"{b['quality']['scores']['gleu']:.6f} |",
            "| Quality evaluated / eligible | "
            f"{a['quality_coverage']['evaluated']}/{a['eligible']} | "
            f"{b['quality_coverage']['evaluated']}/{b['eligible']} |",
            "",
            f"N-gram/baseline p95 ratio: **{value['p95_ratio_ngram_over_baseline']:.3f}**.",
            f"Identical output text on {value['identical_common_outputs']}/"
            f"{value['common_successful_outputs']} common successful inputs.",
            "Common-cohort GLEU with identical reference sampling: "
            f"{value['common_cohort_gleu']['baseline']:.6f} baseline / "
            f"{value['common_cohort_gleu']['ngram']:.6f} n-gram.",
            "",
            "The original 95% within 1 second target stays unchanged. "
            "These observations apply to this workload and deployment; "
            "they do not establish production guarantees.",
            "",
            *["- " + v for v in value["limitations"]],
        ]
    )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--ngram", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = compare(args.baseline, args.ngram)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "comparison.json").write_text(json.dumps(value, indent=2) + "\n")
    (args.output / "comparison.md").write_text(report(value))
