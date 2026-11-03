"""Deterministic fixture builders, never a raw-event measurement or SLO evaluator."""

import math

from platform_contracts import RunResult
from platform_contracts.runs import (
    CustomerMeasurements,
    LatencySummary,
    QualityResult,
    RequestCounts,
    SLOAssessment,
    SLOWindow,
)


def result_for(snapshot, outcome):
    assessments, measurements, quality, customer_counts = [], [], [], {}
    for customer in snapshot.customers:
        eligible = 0
        for target in customer.slo.targets:
            windows = []
            for offset in range(0, snapshot.scenario.measurement_duration_s, customer.slo.window_s):
                count = (
                    0
                    if outcome == "insufficient_data"
                    else max(100, customer.slo.minimum_eligible_requests)
                )
                good = (
                    math.ceil(count * target.minimum_attainment_fraction)
                    if outcome != "violating"
                    else 0
                )
                windows.append(
                    SLOWindow(
                        offset_s=offset,
                        duration_s=min(
                            customer.slo.window_s, snapshot.scenario.measurement_duration_s - offset
                        ),
                        eligible_requests=count,
                        good_requests=good,
                        verdict=outcome
                        if outcome in {"violating", "insufficient_data"}
                        else "passing",
                    )
                )
            eligible = sum(w.eligible_requests for w in windows)
            assessments.append(
                SLOAssessment(
                    customer_ref=customer.ref,
                    metric=target.metric,
                    threshold_ms=target.threshold_ms,
                    minimum_attainment_fraction=target.minimum_attainment_fraction,
                    minimum_eligible_requests=customer.slo.minimum_eligible_requests,
                    windows=windows,
                    verdict=outcome if outcome in {"violating", "insufficient_data"} else "passing",
                )
            )
        counts = RequestCounts(
            offered=eligible,
            attempted=eligible,
            eligible_for_slo=eligible,
            completed=eligible,
            rejected=0,
            failed=0,
            timed_out=0,
            cancelled=0,
        )
        customer_counts[customer.ref] = counts
        measurements.append(
            CustomerMeasurements(
                customer_ref=customer.ref,
                input_tokens=eligible * 32,
                cached_input_tokens=0,
                output_tokens=eligible * 16,
                latencies={
                    t.metric: LatencySummary(
                        sample_count=eligible,
                        p50_ms=t.threshold_ms * 0.4,
                        p95_ms=t.threshold_ms * (1.2 if outcome == "violating" else 0.8),
                        p99_ms=t.threshold_ms * 1.5,
                    )
                    for t in customer.slo.targets
                }
                if eligible
                else {},
            )
        )
        quality.append(
            QualityResult(
                customer_ref=customer.ref,
                evaluator=customer.quality.evaluator,
                status="not_evaluated",
                notes="Synthetic fixture. No quality evaluation was performed.",
            )
        )
    aggregate = RequestCounts(
        **{
            key: sum(getattr(c, key) for c in customer_counts.values())
            for key in RequestCounts.model_fields
        }
    )
    invalid = outcome == "invalid"
    return RunResult(
        validity="invalid" if invalid else "valid",
        invalid_reasons=("Synthetic invalid measurement.",) if invalid else (),
        requests=aggregate,
        customer_requests=customer_counts,
        measurements=measurements,
        slo_assessments=() if invalid else assessments,
        slo_verdict="not_evaluated" if invalid else outcome,
        quality=quality,
        notes="SYNTHETIC FIXTURE. Fabricated counts and latencies; quality not evaluated.",
    )
