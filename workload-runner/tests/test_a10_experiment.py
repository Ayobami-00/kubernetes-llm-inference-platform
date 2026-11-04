import copy
from types import SimpleNamespace

import pytest
from platform_contracts import load_catalog
from platform_contracts.loading import load_document
from platform_contracts.serving import ServingProfile
from workload_runner.experiments.a10 import (
    comparison_scenario,
    paired_schedule,
    schedule_identity,
    token_overlap,
    validate_profiles,
)
from workload_runner.experiments.comparison import bootstrap_delta, quality_coverage


def test_a10_scenario_is_distinct_and_canonical_unchanged(repo):
    source = load_catalog(repo).get("scenario", "writer-assist-baseline@1")
    before = source.model_dump(mode="json")
    short = comparison_scenario(source)
    assert short.ref != source.ref
    assert short.measurement_duration_s == 300
    assert short.warmup.duration_s == 60
    assert short.drain_timeout_s == 30
    assert short.phases[0].arrivals == source.phases[0].arrivals
    assert short.phases[1].arrivals == source.phases[1].arrivals
    assert source.model_dump(mode="json") == before


def test_only_reviewed_speculation_difference_is_allowed(repo):
    profiles = {
        variant: load_document(
            repo / f"deploy/environments/lab/experiments/a10/{variant}.yaml", ServingProfile
        ).model_dump(mode="json")
        for variant in ("baseline", "ngram")
    }
    assert validate_profiles(profiles)["ngram"].engine.speculative_decoding.method == "ngram"
    changed = copy.deepcopy(profiles)
    changed["ngram"]["engine"]["max_num_seqs"] += 1
    with pytest.raises(ValueError, match="controls differ"):
        validate_profiles(changed)
    changed = copy.deepcopy(profiles)
    changed["ngram"]["engine"]["speculative_decoding"]["num_speculative_tokens"] = 6
    with pytest.raises(ValueError, match="fixed n-gram"):
        validate_profiles(changed)


def test_pairing_ignores_only_plan_ids():
    left = [
        {
            "id": "a",
            "customer_ref": "one@1",
            "entry_id": "source",
            "phase": "steady",
            "warmup": False,
            "scheduled_offset_ms": 123.0,
        }
    ]
    right = [{**left[0], "id": "b", "customer_ref": "two@1"}]
    assert paired_schedule(left) == paired_schedule(right)
    assert schedule_identity(left) == schedule_identity(right)
    right[0]["scheduled_offset_ms"] += 0.001
    assert schedule_identity(left) != schedule_identity(right)


def test_copy_spans_do_not_double_count_repeated_tokens():
    metrics = token_overlap([1, 2], [1, 2, 1, 2])
    assert metrics["matched_tokens"] == 2
    assert metrics["output_copy_fraction"] == 0.5
    assert metrics["longest_matching_span"] == 2
    assert metrics["token_edit_distance"] == 2
    assert metrics["normalized_token_edit_distance"] == 0.5
    assert token_overlap([], [1])["output_copy_fraction"] == 0
    assert token_overlap([1], [])["output_copy_fraction"] is None
    assert token_overlap([1, 2, 3], [1, 9, 3])["token_edit_distance"] == 1


def test_quality_failure_missing_and_unscorable_coverage_reconciles():
    sentence = "This is a fully corrected synthetic sentence."
    entries = {
        key: SimpleNamespace(
            turns=[
                SimpleNamespace(
                    messages=[{"content": sentence}],
                    references={"corrections": [sentence] * 4} if key != "unscorable" else {},
                )
            ]
        )
        for key in ("good", "failure", "missing", "unscorable")
    }
    rows = [
        SimpleNamespace(
            source_entry_id=key,
            outcome="failed" if key == "failure" else "completed",
            output_text=None if key == "missing" else sentence,
        )
        for key in entries
    ]
    result = quality_coverage(rows, entries)
    assert result["coverage"] == {
        "eligible": 4,
        "evaluated": 1,
        "failed": 1,
        "missing": 1,
        "unscorable": 1,
    }
    assert result["gleu"] == 1


def test_paired_uncertainty_is_deterministic_and_preserves_pairing():
    result = bootstrap_delta([(400, 300), (500, 400)], iterations=100)
    assert result == bootstrap_delta([(400, 300), (500, 400)], iterations=100)
    assert result["estimate_ms"] == -100
    assert result["percentile_95_interval_ms"] == [-100, -100]
    assert bootstrap_delta([]) is None
