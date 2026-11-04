"""Pure paired WriterAssist analysis. Inputs are verified by the artifact owner."""

import copy
import random
from collections import Counter
from statistics import mean

from slo_evaluator.evaluation import nearest_rank

from workload_runner.experiments.a10 import paired_schedule, token_overlap, validate_profiles
from workload_runner.reporting.quality import corpus_gleu


def percentiles(values):
    return {
        "samples": len(values),
        **{
            f"p{int(p * 100)}_ms": nearest_rank(values, p) if values else None
            for p in (0.5, 0.95, 0.99)
        },
    }


def length_distribution(values):
    return {
        "samples": len(values),
        "min": min(values) if values else None,
        "mean": mean(values) if values else None,
        "p50": nearest_rank(values, 0.5) if values else None,
        "p95": nearest_rank(values, 0.95) if values else None,
        "max": max(values) if values else None,
        "unit": "tokens",
    }


def control_checks(runs, schedules):
    profiles = {
        name: run.snapshot.serving_profiles[0].model_dump(mode="json")
        for name, run in zip(("baseline", "ngram"), runs, strict=True)
    }
    validate_profiles(profiles)
    left, right = (run.snapshot for run in runs)
    if any(
        len(s.customers) != 1 or s.derivation.get(s.customers[0].ref) != "writer-assist@1"
        for s in (left, right)
    ):
        raise ValueError("Only paired WriterAssist runs are supported")
    for field in ("generation", "limits", "slo", "traffic_allowance", "workload"):
        values = [getattr(s.customers[0], field).model_dump(mode="json") for s in (left, right)]
        if field == "workload":
            for value in values:
                value.pop("dataset_ref")
        if values[0] != values[1]:
            raise ValueError(f"Customer {field} controls differ")
    normalized = []
    for snapshot in (left, right):
        target = snapshot.target.model_dump(mode="json")
        target["credential_env"] = sorted(target["credential_env"].values())
        normalized.append(
            {
                "seed": snapshot.scenario.seed,
                "warmup_s": snapshot.scenario.warmup.duration_s,
                "drain_s": snapshot.scenario.drain_timeout_s,
                "phases": [
                    {
                        "id": p.id,
                        "duration_s": p.duration_s,
                        "arrival": next(iter(p.arrivals.values())).model_dump(mode="json"),
                    }
                    for p in snapshot.scenario.phases
                ],
                "delivery": snapshot.delivery_policy.model_dump(mode="json"),
                "versions": snapshot.versions.model_dump(mode="json"),
                "preparation": [p.model_dump(mode="json") for p in snapshot.preparation],
                "target": target,
                "node_uids": sorted(
                    n.kubernetes.uid for n in snapshot.inventory.nodes if n.provider == "lambda"
                ),
            }
        )
    if normalized[0] != normalized[1] or paired_schedule(schedules[0]) != paired_schedule(
        schedules[1]
    ):
        raise ValueError("Traffic, preparation, execution target, pins or GPU identity differ")
    return {
        "same_materialized_arrivals_and_inputs": True,
        "same_serving_settings_except_ngram": True,
        "same_customer_targets_and_delivery_policy": True,
    }


def quality_coverage(rows, entries):
    coverage = dict(eligible=len(rows), evaluated=0, failed=0, missing=0, unscorable=0)
    examples = []
    for row in sorted(rows, key=lambda r: r.source_entry_id):
        if row.outcome != "completed":
            coverage["failed"] += 1
        elif row.output_text is None:
            coverage["missing"] += 1
        else:
            entry = entries[row.source_entry_id]
            refs = entry.turns[0].references.get("corrections")
            if not refs:
                coverage["unscorable"] += 1
            else:
                coverage["evaluated"] += 1
                examples.append((entry.turns[0].messages[-1]["content"], row.output_text, refs))
    if (
        sum(coverage[k] for k in ("evaluated", "failed", "missing", "unscorable"))
        != coverage["eligible"]
    ):
        raise ValueError("Quality coverage does not reconcile")
    return {"coverage": coverage, "gleu": corpus_gleu(examples, seed=0, iterations=500)}


def bootstrap_delta(pairs, iterations=1000, seed=42):
    if not pairs:
        return None
    rng = random.Random(seed)
    deltas = [mean([right - left for left, right in pairs])]
    for _ in range(iterations):
        sample = rng.choices(pairs, k=len(pairs))
        deltas.append(mean(right - left for left, right in sample))
    return {
        "metric": "mean paired completion latency delta, ngram minus baseline, ms",
        "paired_sources": len(pairs),
        "estimate_ms": deltas[0],
        "percentile_95_interval_ms": [
            nearest_rank(deltas[1:], 0.025),
            nearest_rank(deltas[1:], 0.975),
        ],
        "iterations": iterations,
        "seed": seed,
        "limitation": (
            "Conditional descriptive request-pair bootstrap; not repeated deployment-level "
            "evidence."
        ),
    }


def compare_pair(runs, records, schedules, entries, encode):
    controls = control_checks(runs, schedules)
    if any(run.fixture or run.status != "completed" or run.result is None for run in runs):
        raise ValueError("The comparison requires two completed real child runs")
    if any(
        run.snapshot.target.mode != "benchmark"
        or any(prepared.data_kind != "public" for prepared in run.snapshot.preparation)
        for run in runs
    ):
        raise ValueError("Synthetic/stub inputs cannot support the live A10 comparison")
    scored = [[row for row in rows if row.eligible_for_slo] for rows in records]
    by_source = [{r.source_entry_id: r for r in rows} for rows in scored]
    if any(len(mapping) != len(rows) for mapping, rows in zip(by_source, scored, strict=True)):
        raise ValueError("Scored source records were reused within a child")
    common = sorted(set(by_source[0]) & set(by_source[1]))
    common = [
        key
        for key in common
        if all(
            mapping[key].outcome == "completed" and mapping[key].output_text is not None
            for mapping in by_source
        )
    ]
    summaries, overlap_rows = [], []
    for variant, run, rows, all_rows in zip(
        ("baseline", "ngram"), runs, scored, records, strict=True
    ):
        due = [r for r in all_rows if r.turn_index == 0]
        lags = [
            max(0, r.actual_send_offset_ms - r.scheduled_offset_ms)
            for r in due
            if r.actual_send_offset_ms is not None and r.scheduled_offset_ms is not None
        ]
        successful = [r for r in rows if r.outcome == "completed" and r.latency_ms is not None]
        summary = {
            "run_id": run.id,
            "execution_status": run.status,
            "measurement_validity": run.result.validity,
            "invalid_reasons": list(run.result.invalid_reasons),
            "slo_verdict": run.result.slo_verdict,
            "requests": run.result.requests.model_dump(mode="json"),
            "scored_outcomes": {
                outcome: sum(r.outcome == outcome for r in rows)
                for outcome in (
                    "completed",
                    "rejected",
                    "failed",
                    "timed_out",
                    "cancelled",
                    "not_sent",
                )
            },
            "completion_latency": percentiles([r.latency_ms for r in successful]),
            "slo_assessments": [a.model_dump(mode="json") for a in run.result.slo_assessments],
            "quality": quality_coverage(rows, entries),
            "delivery": {
                "due_admissions": len(due),
                "missed_due_admissions": sum(r.outcome == "not_sent" for r in due),
                "late_above_100ms": sum(lag > 100 for lag in lags),
                "maximum_lag_ms": max(lags, default=None),
            },
            "finish_reasons": dict(Counter(r.finish_reason or "unavailable" for r in rows)),
            "token_count_sources": dict(Counter(r.token_count_source for r in rows)),
            "client_ttft": "unavailable: non-streaming comparison",
        }
        for row in rows:
            if row.outcome != "completed" or row.output_text is None:
                continue
            entry = entries[row.source_entry_id]
            source = entry.turns[0].messages[-1]["content"]
            metrics = token_overlap(encode(source), encode(row.output_text))
            overlap_rows.append(
                {
                    "variant": variant,
                    "source_entry_id": row.source_entry_id,
                    "prepared_rendered_input_tokens": entry.turns[0].input_tokens,
                    "completion_latency_ms": row.latency_ms,
                    **metrics,
                }
            )
        own = [r for r in overlap_rows if r["variant"] == variant]
        summary["source_tokens_tokenizer"] = length_distribution([r["source_tokens"] for r in own])
        summary["output_tokens_tokenizer"] = length_distribution([r["output_tokens"] for r in own])
        summary["prepared_rendered_input_tokens"] = length_distribution(
            [r["prepared_rendered_input_tokens"] for r in own]
        )
        summaries.append(summary)
    valid = all(r.result.validity == "valid" for r in runs)
    left, right = (summary["completion_latency"]["p95_ms"] for summary in summaries)
    ratio = right / left if valid and left is not None and left > 0 and right is not None else None
    common_quality = []
    for mapping in by_source:
        common_quality.append(quality_coverage([mapping[key] for key in common], entries))
    paired_latencies = [
        (by_source[0][key].latency_ms, by_source[1][key].latency_ms)
        for key in common
        if all(mapping[key].latency_ms is not None for mapping in by_source)
    ]
    bins = []
    left_overlap = {r["source_entry_id"]: r for r in overlap_rows if r["variant"] == "baseline"}
    for name, lower, upper in (
        ("below 50%", 0, 0.5),
        ("50–80%", 0.5, 0.8),
        ("at least 80%", 0.8, 1.000001),
    ):
        keys = [
            key
            for key in common
            if key in left_overlap
            and left_overlap[key]["output_copy_fraction"] is not None
            and lower <= left_overlap[key]["output_copy_fraction"] < upper
        ]
        deltas = [
            by_source[1][key].latency_ms - by_source[0][key].latency_ms
            for key in keys
            if all(mapping[key].latency_ms is not None for mapping in by_source)
        ]
        bins.append(
            {
                "baseline_source_copy_fraction": name,
                "samples": len(deltas),
                "mean_latency_delta_ngram_minus_baseline_ms": mean(deltas) if deltas else None,
            }
        )
    review = [
        {
            "source_entry_id": key,
            "source": entries[key].turns[0].messages[-1]["content"],
            "references": entries[key].turns[0].references["corrections"],
            "baseline": by_source[0][key].output_text,
            "ngram": by_source[1][key].output_text,
        }
        for key in common
        if by_source[0][key].output_text != by_source[1][key].output_text
    ]
    return {
        "schema_version": "a10-comparison.v1",
        "controls": controls,
        "performance_comparable": valid,
        "p95_ratio_ngram_over_baseline": ratio,
        "p95_reduction_percent": (1 - ratio) * 100 if ratio is not None else None,
        "latency_changes": {
            field: {
                "ngram_minus_baseline_ms": summaries[1]["completion_latency"][field]
                - summaries[0]["completion_latency"][field],
                "reduction_percent": 100
                * (
                    1
                    - summaries[1]["completion_latency"][field]
                    / summaries[0]["completion_latency"][field]
                ),
            }
            for field in ("p50_ms", "p95_ms", "p99_ms")
            if valid
            and summaries[0]["completion_latency"][field]
            and summaries[1]["completion_latency"][field] is not None
        },
        "conclusion": "inconclusive: invalid workload delivery"
        if not valid
        else "promising p95 reduction in this pair"
        if ratio is not None and ratio < 1
        else "no p95 reduction demonstrated in this pair",
        "baseline": summaries[0],
        "ngram": summaries[1],
        "common_cohort": {
            "samples": len(common),
            "identical_outputs": len(common) - len(review),
            "baseline_quality": common_quality[0],
            "ngram_quality": common_quality[1],
            "gleu_reference_sampling": "Same sorted source-ID order, seed 0, 500 iterations",
        },
        "paired_latency_uncertainty": bootstrap_delta(paired_latencies) if valid else None,
        "overlap_method": (
            "Pinned tokenizer, no special tokens; difflib.SequenceMatcher(autojunk=False) "
            "ordered disjoint matching blocks; matched/output tokens. Edit distance is "
            "Levenshtein/max(source length, output length, 1). Source sentence excludes "
            "system and chat-template tokens; full rendered input length is preparation "
            "metadata, not source length. Full-prompt overlap is not measured."
        ),
        "overlap_groups": bins,
        "per_request_overlap": overlap_rows,
        "private_disagreements": review,
        "limitations": [
            "One baseline-first pair, no randomized deployment repetitions; "
            "order and warm-state effects remain.",
            "Successful-request percentiles have explicit coverage; "
            "all eligible failures remain in SLO attainment.",
            "Public JFLEG task proxy, not private customer traces; "
            "measured lengths do not cover all allowed inputs.",
            "Copy fraction and aggregate acceptance are diagnostic; "
            "they do not establish per-request acceptance or causation.",
            "Private single-A10 experiment with retained security findings; "
            "no serving HA or production capacity promise.",
        ],
    }


def public_comparison(value):
    result = copy.deepcopy(value)
    result.pop("private_disagreements", None)
    result.pop("per_request_overlap", None)
    return result
