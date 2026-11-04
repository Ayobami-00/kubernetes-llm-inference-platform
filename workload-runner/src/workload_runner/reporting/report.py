"""Readable report generation from validated historical results."""

import json

from workload_runner.artifacts import read_jsonl


def write_report(run, directory):
    result = run.result
    lines = [
        f"# Workload run {run.id}",
        "",
        "**SYNTHETIC / EMULATED — local stub; no model performance claims.**"
        if run.fixture
        else "**Live benchmark — hardware and preparation metadata are recorded in the snapshot.**",
        "",
        f"Execution: **{run.status}**",
        f"Measurement validity: **{result.validity if result else 'unavailable'}**",
        f"Performance verdict: **{result.slo_verdict if result else 'not_evaluated'}**",
        f"Serving stack: `{run.snapshot.target.serving_stack}`" if run.snapshot.target else "",
        f"Client endpoint: `{run.snapshot.target.endpoint}`" if run.snapshot.target else "",
        "",
        "Phases and windows use the frozen scenario. Percentiles use nearest rank. "
        "Warmup cohorts and drain sends are unscored; failures count against attainment.",
        "",
    ]
    if result:
        lines += [
            "| Customer | Offered | Eligible | Completed | Quality |",
            "|---|---:|---:|---:|---|",
        ]
        for ref, counts in result.customer_requests.items():
            quality = next(
                (q.status for q in result.quality if q.customer_ref == ref), "not_evaluated"
            )
            lines.append(
                f"| {ref} | {counts.offered} | {counts.eligible_for_slo} | "
                f"{counts.completed} | {quality} |"
            )
        requests = list(read_jsonl(directory / "requests.jsonl"))
        due = [r for r in requests if r["turn_index"] == 0]
        lags = [
            max(0.0, r["actual_send_offset_ms"] - r["scheduled_offset_ms"])
            for r in due
            if r["actual_send_offset_ms"] is not None
        ]
        lag_text = f"{max(lags):.3f} ms" if lags else "unavailable"
        lines += [
            "",
            f"Delivery: {len(due)} planned admissions; "
            f"{sum(r['outcome'] == 'not_sent' for r in due)} missed; "
            f"{lag_text} maximum dispatch lag.",
            f"Delivery policy: lag ≤ {run.snapshot.delivery_policy.max_scheduling_lag_ms} ms "
            f"for at least {(1 - run.snapshot.delivery_policy.max_late_fraction) * 100:g}% "
            "of due admissions; no missed admissions.",
        ]
        clock = directory / "engine-clock.json"
        if clock.exists():
            lines += [
                "",
                "Common timeline origin: `" + json.dumps(json.loads(clock.read_text())) + "`",
            ]
        lines += ["", "Session outcomes:", ""]
        for ref, counts in result.customer_sessions.items():
            lines.append(
                f"- {ref}: {counts.started} sessions started; {counts.completed} completed, "
                f"{counts.failed} failed, {counts.cancelled} cancelled, {counts.cutoff} cut off."
            )
        lines += ["", "Preparation and proxy limitations:", ""]
        for prep in run.snapshot.preparation:
            lines.append(
                f"- {prep.customer_ref}: {prep.data_kind}; {prep.entry_count} selected, "
                f"{prep.warmup_count} warmup. Exclusions: {dict(prep.exclusions)}. "
                f"Adaptations: {dict(prep.adaptations)}. Licenses: {dict(prep.licenses)}."
            )
        lines += [
            "",
            "Invalidity reasons: " + (", ".join(result.invalid_reasons) or "none"),
            "",
            "Quality coverage and reference review bundles: `quality.json`. Raw engine aggregates "
            "are diagnostic; `run.json` contains the canonical assessment.",
        ]
        if result.slo_assessments:
            lines += [
                "",
                "| Customer | Metric | Threshold ms | Window s | Good / eligible | Verdict |",
                "|---|---|---:|---|---:|---|",
            ]
            for assessment in result.slo_assessments:
                for window in assessment.windows:
                    lines.append(
                        f"| {assessment.customer_ref} | {assessment.metric} | "
                        f"{assessment.threshold_ms:g} | {window.offset_s}–"
                        f"{window.offset_s + window.duration_s} | "
                        f"{window.good_requests} / {window.eligible_requests} | {window.verdict} |"
                    )
        lines += ["", "Quality scores (coverage and unscorable outputs are in `quality.json`):", ""]
        for quality in result.quality:
            text = ", ".join(f"{name}={value:.4f}" for name, value in quality.scores.items())
            lines.append(f"- {quality.customer_ref}: {text or quality.status}. {quality.notes}")
    if run.error:
        lines += ["", f"Execution error: {run.error.code} — {run.error.message}"]
    (directory / "report.md").write_text("\n".join(lines) + "\n")
