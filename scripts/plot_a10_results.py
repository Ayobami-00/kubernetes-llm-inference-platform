"""Custom Matplotlib plots from public, aggregate-only measured A10 results."""

import argparse
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "experiments/writer-assist-a10/results/2026-11-04-summary.json"
OUTPUT = ROOT / "docs/assets/experiments"
VARIANTS = ("baseline", "ngram")
COLORS = ("#536D8C", "#078E79")
INK = "#182C3E"
MUTED = "#516579"


def validate_summary(data):
    """Fail closed on inconsistent public inputs; do not reevaluate private records."""
    if data["schema_version"] != "a10-public-summary.v1" or not data["performance_comparable"]:
        raise ValueError("Figures require a comparable measured pair")
    for variant in VARIANTS:
        row = data["variants"][variant]
        if row["execution_status"] != "completed" or row["measurement_validity"] != "valid":
            raise ValueError("Both child executions must have valid measurements")
        eligible = row["eligible_requests"]
        outcomes = row["scored_outcomes"]
        if (
            type(eligible) is not int
            or eligible < data["slo"]["minimum_eligible_requests"]
            or any(type(v) is not int or v < 0 for v in outcomes.values())
            or sum(outcomes.values()) != eligible
            or row["latency"]["samples"] != outcomes["completed"]
            or not 0 <= row["within_threshold"] <= eligible
        ):
            raise ValueError("Scored outcomes, SLO counts and latency coverage must reconcile")
        quantiles = [row["latency"][key] for key in ("p50_ms", "p95_ms", "p99_ms")]
        if any(not math.isfinite(v) or v <= 0 for v in quantiles) or quantiles != sorted(quantiles):
            raise ValueError("Completion latency quantiles must be finite, positive and ordered")
        coverage = row["quality"]["coverage"]
        if (
            coverage["eligible"] != eligible
            or any(type(v) is not int or v < 0 for v in coverage.values())
            or sum(coverage[k] for k in ("evaluated", "failed", "missing", "unscorable"))
            != eligible
            or not 0 <= row["quality"]["gleu"] <= 1
        ):
            raise ValueError("Quality coverage and score must reconcile")
        passing = row["within_threshold"] / eligible >= data["slo"]["minimum_attainment_fraction"]
        if (row["slo_verdict"] == "passing") != passing:
            raise ValueError("Published SLO verdict differs from its attainment counts")
    if data["variants"]["baseline"]["speculation"]["acceptance_fraction"] is not None:
        raise ValueError("Baseline speculative acceptance is unavailable, not zero")
    speculation = data["variants"]["ngram"]["speculation"]
    accepted, proposed = speculation["accepted_tokens"], speculation["proposed_tokens"]
    if (
        not 0 <= accepted <= proposed
        or proposed <= 0
        or not math.isclose(speculation["acceptance_fraction"], accepted / proposed, abs_tol=1e-12)
    ):
        raise ValueError("Speculative acceptance must match the published counter estimates")
    left, right = (data["variants"][v]["latency"]["p95_ms"] for v in VARIANTS)
    reduction = 100 * (1 - right / left)
    if not math.isclose(reduction, data["p95_reduction_percent"], abs_tol=1e-9):
        raise ValueError("Published p95 reduction differs from the measured quantiles")
    return data


def load_summary(path=SUMMARY):
    return validate_summary(json.loads(Path(path).read_text()))


def axes_style(axis):
    axis.set_axisbelow(True)
    axis.yaxis.grid(True, color="#E5ECF1", linewidth=0.8)
    axis.tick_params(axis="both", length=0, labelcolor=MUTED, pad=9)
    for side in ("top", "right", "left"):
        axis.spines[side].set_visible(False)
    axis.spines["bottom"].set_color("#CCD7DF")


def cohort_label(rows):
    counts = [r["eligible_requests"] for r in rows]
    if counts[0] == counts[1]:
        return f"{counts[0]} scored requests per variant"
    return f"{counts[0]} baseline / {counts[1]} n-gram scored requests"


def save(fig, output, name):
    # Fixed SVG metadata/hash salt avoids random element IDs and timestamps.
    for extension in ("png", "svg"):
        metadata = (
            {"Date": "2026-11-04", "Creator": "KLIP custom Matplotlib plotting script"}
            if extension == "svg"
            else {"Software": "KLIP custom Matplotlib plotting script"}
        )
        fig.savefig(output / f"{name}.{extension}", dpi=200, metadata=metadata, facecolor="white")


def headline(data, plt, output):
    rows = [data["variants"][v] for v in VARIANTS]
    values = [r["latency"]["p95_ms"] for r in rows]
    reduction = data["p95_reduction_percent"]
    movement = "lower" if reduction >= 0 else "higher"
    fig = plt.figure(figsize=(11.5, 7.3), facecolor="white")
    fig.text(0.075, 0.94, "WriterAssist on one A10", fontsize=23, weight="bold", color=INK)
    fig.text(
        0.075,
        0.89,
        "Qwen3-4B · baseline decoding vs n-gram prompt lookup",
        fontsize=13,
        color=MUTED,
    )
    fig.text(
        0.075,
        0.815,
        f"{abs(reduction):.1f}% {movement} p95 completion latency",
        fontsize=22,
        weight="bold",
        color=COLORS[1] if reduction >= 0 else "#A34B2F",
    )
    axis = fig.add_axes((0.105, 0.235, 0.825, 0.51))
    axes_style(axis)
    threshold = data["slo"]["threshold_ms"]
    axis.set_ylim(0, max(threshold, *values) * 1.15)
    axis.set_xlim(-0.65, 1.65)
    axis.set_ylabel("Client completion latency, p95 (ms)", labelpad=13, color=MUTED)
    axis.set_xticks((0, 1), ("Baseline", "N-gram"))
    axis.bar((0, 1), values, width=0.48, color=COLORS, zorder=3)
    for position, value in enumerate(values):
        axis.text(
            position,
            value + threshold * 0.025,
            f"{value:.0f} ms",
            ha="center",
            va="bottom",
            fontsize=18,
            weight="bold",
            color=INK,
        )
    axis.axhline(threshold, color="#AA6226", linestyle=(0, (5, 4)), linewidth=1.4)
    axis.text(
        1.63,
        threshold * 1.02,
        f"{threshold / 1000:g}-second SLO threshold",
        ha="right",
        va="bottom",
        fontsize=11,
        color="#96531E",
    )
    attainment = " · ".join(
        f"{name} {r['within_threshold']}/{r['eligible_requests']} "
        f"({100 * r['within_threshold'] / r['eligible_requests']:.2f}%)"
        for name, r in zip(("baseline", "n-gram"), rows, strict=True)
    )
    fig.text(0.105, 0.145, "Within 1 second: " + attainment, fontsize=11, color=MUTED)
    errors = sum(r["eligible_requests"] - r["scored_outcomes"]["completed"] for r in rows)
    fig.text(
        0.075,
        0.078,
        f"One paired run · {cohort_label(rows)} · valid delivery · {errors} scored failures",
        fontsize=10,
        color=MUTED,
    )
    fig.text(
        0.075,
        0.045,
        "Nearest-rank p95; zero-based axis. Baseline ran first. "
        "No deployment-level uncertainty estimate.",
        fontsize=9.5,
        color=MUTED,
    )
    save(fig, output, "a10-p95-comparison")
    plt.close(fig)


def supporting(data, plt, output):
    rows = [data["variants"][v] for v in VARIANTS]
    fig, axes = plt.subplots(1, 3, figsize=(14, 5.8), facecolor="white")
    fig.subplots_adjust(left=0.065, right=0.98, top=0.73, bottom=0.24, wspace=0.4)
    fig.text(
        0.065,
        0.93,
        "Latency, SLO attainment and correction quality",
        fontsize=21,
        color=INK,
        weight="bold",
    )
    fig.text(
        0.065,
        0.865,
        "Same selected JFLEG inputs and arrivals · " + cohort_label(rows) + " · single A10",
        fontsize=12,
        color=MUTED,
    )
    for axis in axes:
        axes_style(axis)
    latency, slo, quality = axes
    for index, (row, color, name) in enumerate(
        zip(rows, COLORS, ("Baseline", "N-gram"), strict=True)
    ):
        values = [row["latency"][f"p{p}_ms"] for p in (50, 95, 99)]
        locations = [x + (index - 0.5) * 0.34 for x in range(3)]
        bars = latency.bar(locations, values, width=0.32, color=color, label=name, zorder=3)
        latency.bar_label(bars, fmt="%.0f", padding=4, fontsize=10, color=INK)
    latency.set_xticks((0, 1, 2), ("p50", "p95", "p99"))
    latency.set_ylabel("Client completion (ms)", color=MUTED)
    latency.set_ylim(0, max(r["latency"]["p99_ms"] for r in rows) * 1.2)
    latency.legend(frameon=False, fontsize=10, loc="upper left")
    latency.set_title("Completion latency", loc="left", fontsize=13, color=INK, pad=15)
    attainment = [100 * r["within_threshold"] / r["eligible_requests"] for r in rows]
    bars = slo.bar((0, 1), attainment, width=0.55, color=COLORS, zorder=3)
    slo.bar_label(bars, fmt="%.2f%%", padding=5, fontsize=11, color=INK)
    target = data["slo"]["minimum_attainment_fraction"] * 100
    slo.axhline(target, linestyle="--", linewidth=1.2, color="#AA6226")
    slo.set_ylim(0, 112)
    slo.set_yticks((0, 25, 50, 75, 100))
    slo.set_ylabel("Eligible requests within 1 second (%)", color=MUTED)
    slo.set_xticks((0, 1), ("Baseline", "N-gram"))
    slo.set_title(
        f"SLO attainment (≥{target:g}% target)", loc="left", fontsize=13, color=INK, pad=15
    )
    bars = quality.bar(
        (0, 1), [r["quality"]["gleu"] for r in rows], width=0.55, color=COLORS, zorder=3
    )
    quality.bar_label(bars, fmt="%.6f", padding=5, fontsize=11, color=INK)
    quality.set_ylim(0, 1)
    quality.set_ylabel("Corpus GLEU (0–1)", color=MUTED)
    quality.set_xticks((0, 1), ("Baseline", "N-gram"))
    quality.set_title("Reference-based quality", loc="left", fontsize=13, color=INK, pad=15)
    fig.text(
        0.065,
        0.12,
        "Quality: four references; identical source order, seed 0, 500 iterations. Scorable: "
        + " vs ".join(
            f"{r['quality']['coverage']['evaluated']}/{r['eligible_requests']}" for r in rows
        )
        + ".",
        fontsize=10,
        color=MUTED,
    )
    fig.text(
        0.065,
        0.065,
        "One baseline-first pair. The small GLEU decrease does not establish quality equivalence. "
        "No uncertainty bars are inferred from aggregate quantiles.",
        fontsize=10,
        color=MUTED,
    )
    save(fig, output, "a10-latency-slo-quality")
    plt.close(fig)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, default=SUMMARY)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    data = load_summary(args.summary)
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 11,
            "svg.fonttype": "none",
            "svg.hashsalt": "klip-a10",
        }
    )
    args.output.mkdir(parents=True, exist_ok=True)
    headline(data, plt, args.output)
    supporting(data, plt, args.output)
    print(f"Wrote two PNG/SVG figure pairs to {args.output}")


if __name__ == "__main__":
    main()
