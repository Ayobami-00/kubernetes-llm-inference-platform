from pathlib import Path

import pytest
from platform_contracts import (
    RequestRecord,
    RunSnapshot,
    ServingBinding,
    SessionRecord,
    build_run_snapshot,
    load_catalog,
)
from slo_evaluator import evaluate, nearest_rank


@pytest.fixture
def snapshot():
    root = Path(__file__).resolve().parents[3]
    catalog = load_catalog(root)
    snapshot = build_run_snapshot(
        catalog,
        "writer-assist-baseline@1",
        (
            ServingBinding(
                customer_ref="writer-assist@1",
                serving_profile_ref="gec-baseline@1",
                worker_group_ref="latency-workers@1",
                deployment_id="gec",
            ),
        ),
    )
    data = snapshot.model_dump(mode="json")
    data["customers"][0]["slo"]["minimum_eligible_requests"] = 2
    return RunSnapshot.model_validate(data)


def record(index, *, offset=None, outcome="completed", latency=100.0, warmup=False):
    offset = (60000.0 + (index // 2) * 300000.0 + (index % 2) * 100.0) if offset is None else offset
    return RequestRecord(
        run_id="test",
        request_id=f"r-{index}",
        customer_ref="writer-assist@1",
        source_entry_id=f"input-{index}",
        session_id=f"s-{index}",
        turn_index=0,
        warmup_cohort=warmup,
        phase="warmup" if warmup else "measurement",
        scheduled_offset_ms=offset,
        actual_send_offset_ms=offset,
        completed_offset_ms=offset + (latency or 100.0),
        latency_ms=latency,
        outcome=outcome,
        complete_output=outcome == "completed",
        eligible_for_slo=not warmup,
        input_tokens=None,
        output_tokens=None,
    )


@pytest.mark.parametrize(
    ("outcome", "latency", "expected"),
    [
        ("completed", 100.0, "passing"),
        ("completed", 2000.0, "violating"),
        ("rejected", None, "violating"),
        ("failed", None, "violating"),
        ("timed_out", None, "violating"),
        ("cancelled", None, "violating"),
    ],
)
def test_classification_and_reconciliation(snapshot, outcome, latency, expected):
    result = evaluate(snapshot, [record(i, outcome=outcome, latency=latency) for i in range(4)], [])
    assert result.slo_verdict == expected and result.validity == "valid"
    assert result.requests.offered == result.requests.attempted == 4
    assert result.requests.eligible_for_slo == 4
    assert sum(w.eligible_requests for w in result.slo_assessments[0].windows) == 4
    assert result.measurements[0].input_tokens is None


def test_warmup_and_window_boundaries(snapshot):
    records = [record(i) for i in range(4)] + [record(9, offset=59999.0, warmup=True)]
    records[2] = record(2, offset=360000.0)
    result = evaluate(snapshot, records, [])
    assert result.slo_verdict == "passing"
    assert [w.eligible_requests for w in result.slo_assessments[0].windows] == [2, 2]
    assert result.requests.offered == 5 and result.requests.eligible_for_slo == 4


def test_insufficient_and_invalid_measurements_cannot_pass(snapshot):
    assert evaluate(snapshot, [record(0)], []).slo_verdict == "insufficient_data"
    result = evaluate(snapshot, [record(i, latency=None) for i in range(4)], [])
    assert result.validity == "invalid" and not result.slo_assessments
    assert result.slo_verdict == "not_evaluated"
    result = evaluate(snapshot, [record(0), record(0)], [])
    assert "duplicate_request_identity" in result.invalid_reasons


def test_generator_delivery_policy(snapshot):
    records = [record(i) for i in range(4)]
    records[0] = records[0].model_copy(update={"scheduled_offset_ms": 59000.0})
    assert "excessive_scheduling_lag" in evaluate(snapshot, records, []).invalid_reasons
    unsent = record(10).model_copy(
        update={
            "outcome": "not_sent",
            "actual_send_offset_ms": None,
            "completed_offset_ms": None,
            "eligible_for_slo": False,
        }
    )
    result = evaluate(snapshot, [*records[1:], unsent], [])
    assert result.validity == "invalid" and result.requests.not_sent == 1


def test_session_denominator_separate_from_request_denominator(snapshot):
    data = snapshot.model_dump(mode="json")
    data["customers"][0]["workload"]["interaction"] = "session"
    data["customers"][0]["slo"]["targets"][0]["metric"] = "session_latency_ms"
    # Validate result against frozen targets after using a supported session profile.
    catalog = load_catalog(Path(__file__).resolve().parents[3])
    session_snapshot = build_run_snapshot(
        catalog,
        "tutor-chat-baseline@1",
        (
            ServingBinding(
                customer_ref="tutor-chat@1",
                serving_profile_ref="shared-small-baseline@1",
                worker_group_ref="shared-workers@1",
                deployment_id="small",
            ),
        ),
    )
    data = session_snapshot.model_dump(mode="json")
    data["customers"][0]["slo"]["targets"][0]["metric"] = "session_latency_ms"
    data["customers"][0]["slo"]["minimum_eligible_requests"] = 1
    session_snapshot = RunSnapshot.model_validate(data)
    requests = [
        record(i).model_copy(
            update={
                "customer_ref": "tutor-chat@1",
                "session_id": f"s-{i // 2}",
                "source_entry_id": f"input-{i // 2}",
                "turn_index": i % 2,
            }
        )
        for i in range(4)
    ]
    sessions = [
        SessionRecord(
            id=f"s-{i}",
            customer_ref="tutor-chat@1",
            source_entry_id=f"input-{i}",
            warmup_cohort=False,
            scheduled_offset_ms=60000.0 + i * 300000,
            started_offset_ms=60000.0 + i * 300000,
            ended_offset_ms=60100.0 + i * 300000,
            expected_turns=2,
            request_ids=(f"r-{i * 2}", f"r-{i * 2 + 1}"),
            outcome="completed",
        )
        for i in range(2)
    ]
    result = evaluate(session_snapshot, requests, sessions)
    assert result.slo_verdict == "passing"
    assert result.customer_sessions["tutor-chat@1"].eligible_for_slo == 2
    assert result.requests.eligible_for_slo == 4


def test_nearest_rank():
    assert nearest_rank([4.0, 1.0, 3.0, 2.0], 0.5) == 2
    assert nearest_rank([4.0, 1.0, 3.0, 2.0], 0.95) == 4
    with pytest.raises(ValueError):
        nearest_rank([], 0.5)
