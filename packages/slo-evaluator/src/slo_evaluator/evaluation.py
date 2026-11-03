"""Pure evaluation of normalized events; no networking, clocks, databases or engine imports."""

import math
from collections import Counter

from platform_contracts import RequestRecord, RunResult, RunSnapshot, SessionCounts, SessionRecord
from platform_contracts.runs import CustomerMeasurements, LatencySummary, RequestCounts


def nearest_rank(values: list[float], percentile: float) -> float:
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be in (0, 1]")
    if not values:
        raise ValueError("cannot calculate a percentile without samples")
    return sorted(values)[max(0, math.ceil(percentile * len(values)) - 1)]


def eligible(snapshot: RunSnapshot, record: RequestRecord) -> bool:
    start = snapshot.scenario.warmup.duration_s * 1000
    end = start + snapshot.scenario.measurement_duration_s * 1000
    return (
        not record.warmup_cohort
        and record.actual_send_offset_ms is not None
        and start <= record.actual_send_offset_ms < end
    )


def _counts(records, scored):
    outcomes = Counter(record.outcome for record in records)
    return RequestCounts(
        offered=len(records),
        attempted=len(records) - outcomes["not_sent"],
        eligible_for_slo=len(scored),
        **{
            name: outcomes[name]
            for name in ("completed", "rejected", "failed", "timed_out", "cancelled", "not_sent")
        },
    )


def evaluate(
    snapshot: RunSnapshot,
    records: list[RequestRecord],
    sessions: list[SessionRecord],
    *,
    quality=(),
    artifacts=None,
    invalid_reasons=(),
) -> RunResult:
    reasons = list(invalid_reasons)
    customers = {customer.ref: customer for customer in snapshot.customers}
    seen = set()
    clean = []
    for record in records:
        if record.request_id in seen:
            reasons.append("duplicate_request_identity")
            continue
        seen.add(record.request_id)
        if record.customer_ref not in customers:
            reasons.append("unknown_customer")
            continue
        if record.eligible_for_slo != eligible(snapshot, record):
            reasons.append("incorrect_scoring_eligibility")
        clean.append(record)
    records = clean
    session_ids = set()
    clean_sessions = []
    by_request = {r.request_id: r for r in records}
    covered = set()
    for session in sessions:
        if session.id in session_ids:
            reasons.append("duplicate_session_identity")
            continue
        session_ids.add(session.id)
        if session.customer_ref not in customers:
            reasons.append("unknown_session_customer")
            continue
        members = [by_request[key] for key in session.request_ids if key in by_request]
        if len(members) != len(session.request_ids):
            reasons.append("missing_session_request")
        if covered & set(session.request_ids):
            reasons.append("request_in_multiple_sessions")
        covered.update(session.request_ids)
        if any(
            r.session_id != session.id
            or r.customer_ref != session.customer_ref
            or r.source_entry_id != session.source_entry_id
            or r.warmup_cohort != session.warmup_cohort
            for r in members
        ):
            reasons.append("session_request_identity_mismatch")
        if [r.turn_index for r in members] != list(range(len(members))):
            reasons.append("unordered_session_requests")
        if session.outcome == "completed" and any(r.outcome != "completed" for r in members):
            reasons.append("completed_session_contains_unsuccessful_request")
        if members and session.started_offset_ms != members[0].actual_send_offset_ms:
            reasons.append("session_start_disagrees_with_request")
        clean_sessions.append(session)
    sessions = clean_sessions
    if sessions:
        if covered != {r.request_id for r in records if r.outcome != "not_sent"}:
            reasons.append("session_request_coverage_mismatch")
    elif any(customers[r.customer_ref].workload.interaction == "session" for r in records):
        reasons.append("missing_session_accounting")
    if any(record.outcome == "not_sent" for record in records):
        reasons.append("missed_due_admission")
    due = [
        r
        for r in records
        if r.actual_send_offset_ms is not None
        and r.scheduled_offset_ms is not None
        and r.turn_index == 0
    ]
    late = sum(
        r.actual_send_offset_ms - r.scheduled_offset_ms
        > snapshot.delivery_policy.max_scheduling_lag_ms
        for r in due
    )
    if due and late / len(due) > snapshot.delivery_policy.max_late_fraction:
        reasons.append("excessive_scheduling_lag")
    start_ms = snapshot.scenario.warmup.duration_s * 1000
    end_ms = start_ms + snapshot.scenario.measurement_duration_s * 1000
    counts = {}
    session_counts = {}
    assessments = []
    measurements = []
    for ref, customer in customers.items():
        owned = [r for r in records if r.customer_ref == ref]
        scored = [r for r in owned if eligible(snapshot, r)]
        counts[ref] = _counts(owned, scored)
        own_sessions = [s for s in sessions if s.customer_ref == ref]
        session_scored = [
            s
            for s in own_sessions
            if not s.warmup_cohort
            and s.started_offset_ms is not None
            and start_ms <= s.started_offset_ms < end_ms
        ]
        outcomes = Counter(s.outcome for s in own_sessions)
        session_counts[ref] = SessionCounts(
            started=sum(s.started_offset_ms is not None for s in own_sessions),
            completed=outcomes["completed"],
            failed=outcomes["failed"],
            cancelled=outcomes["cancelled"],
            cutoff=outcomes["cutoff"],
            eligible_for_slo=len(session_scored),
        )
        latencies = {}
        for metric, attribute in (
            ("completion_latency_ms", "latency_ms"),
            ("ttft_ms", "ttft_ms"),
            ("tpot_ms", "tpot_ms"),
        ):
            if metric != "completion_latency_ms" and not customer.generation.streaming:
                continue
            values = [
                getattr(r, attribute)
                for r in scored
                if r.outcome == "completed" and getattr(r, attribute) is not None
            ]
            if values:
                latencies[metric] = LatencySummary(
                    sample_count=len(values),
                    p50_ms=nearest_rank(values, 0.5),
                    p95_ms=nearest_rank(values, 0.95),
                    p99_ms=nearest_rank(values, 0.99),
                )
        usage = {}
        for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
            values = [getattr(r, field) for r in scored]
            usage[field] = sum(values) if values and all(v is not None for v in values) else None
        measurements.append(CustomerMeasurements(customer_ref=ref, latencies=latencies, **usage))
        for target in customer.slo.targets:
            is_session = target.metric == "session_latency_ms"
            samples = session_scored if is_session else scored
            windows = []
            for offset in range(0, snapshot.scenario.measurement_duration_s, customer.slo.window_s):
                duration = min(
                    customer.slo.window_s, snapshot.scenario.measurement_duration_s - offset
                )
                window_samples = [
                    sample
                    for sample in samples
                    if start_ms + offset * 1000
                    <= (sample.started_offset_ms if is_session else sample.actual_send_offset_ms)
                    < start_ms + (offset + duration) * 1000
                ]
                good = 0
                for sample in window_samples:
                    if sample.outcome != "completed":
                        continue
                    if is_session:
                        latency = (
                            sample.ended_offset_ms - sample.started_offset_ms
                            if sample.ended_offset_ms is not None
                            else None
                        )
                    else:
                        latency = getattr(
                            sample,
                            {
                                "completion_latency_ms": "latency_ms",
                                "ttft_ms": "ttft_ms",
                                "tpot_ms": "tpot_ms",
                            }[target.metric],
                        )
                        if not sample.complete_output:
                            continue
                    if latency is None:
                        reasons.append(f"missing_required_timing:{ref}:{target.metric}")
                    elif latency <= target.threshold_ms:
                        good += 1
                n = len(window_samples)
                verdict = (
                    "insufficient_data"
                    if n < customer.slo.minimum_eligible_requests
                    else "passing"
                    if good / n >= target.minimum_attainment_fraction
                    else "violating"
                )
                windows.append(
                    dict(
                        offset_s=offset,
                        duration_s=duration,
                        eligible_requests=n,
                        good_requests=good,
                        verdict=verdict,
                    )
                )
            verdict = (
                "violating"
                if any(w["verdict"] == "violating" for w in windows)
                else "insufficient_data"
                if any(w["verdict"] == "insufficient_data" for w in windows)
                else "passing"
            )
            assessments.append(
                dict(
                    customer_ref=ref,
                    metric=target.metric,
                    threshold_ms=target.threshold_ms,
                    minimum_attainment_fraction=target.minimum_attainment_fraction,
                    minimum_eligible_requests=customer.slo.minimum_eligible_requests,
                    windows=windows,
                    verdict=verdict,
                )
            )
    total = {
        field: sum(getattr(c, field) for c in counts.values())
        for field in RequestCounts.model_fields
    }
    reasons = sorted(set(reasons))
    overall = (
        "not_evaluated"
        if reasons
        else "violating"
        if any(a["verdict"] == "violating" for a in assessments)
        else "insufficient_data"
        if any(a["verdict"] == "insufficient_data" for a in assessments)
        else "passing"
    )
    return RunResult(
        validity="invalid" if reasons else "valid",
        invalid_reasons=reasons,
        requests=total,
        customer_requests=counts,
        customer_sessions=session_counts,
        measurements=measurements,
        slo_assessments=[] if reasons else assessments,
        slo_verdict=overall,
        quality=quality,
        artifacts=artifacts or {},
    )
