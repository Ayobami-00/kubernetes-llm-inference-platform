"""Public figures must preserve counts, unknown metrics and observed arithmetic."""

import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "plot_a10_results", ROOT / "scripts/plot_a10_results.py"
)
plots = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plots)


def test_public_summary_matches_recorded_pair():
    data = plots.load_summary()
    assert data["p95_reduction_percent"] == pytest.approx(20.437022510027592)
    assert data["variants"]["baseline"]["within_threshold"] == 214
    assert data["variants"]["ngram"]["within_threshold"] == 219
    assert data["common_cohort"]["samples"] == 220
    assert data["variants"]["baseline"]["speculation"]["acceptance_fraction"] is None


@pytest.mark.parametrize(
    "fault", ["validity", "percentage", "denominator", "coverage", "baseline-zero", "acceptance"]
)
def test_public_plot_rejects_misleading_summary(fault):
    data = copy.deepcopy(plots.load_summary())
    baseline = data["variants"]["baseline"]
    if fault == "validity":
        baseline["measurement_validity"] = "invalid"
    elif fault == "percentage":
        data["p95_reduction_percent"] = 42
    elif fault == "denominator":
        baseline["eligible_requests"] -= 1
    elif fault == "coverage":
        baseline["quality"]["coverage"]["evaluated"] -= 1
    elif fault == "baseline-zero":
        baseline["speculation"]["acceptance_fraction"] = 0
    else:
        data["variants"]["ngram"]["speculation"]["acceptance_fraction"] = 0.99
    with pytest.raises(ValueError):
        plots.validate_summary(data)
