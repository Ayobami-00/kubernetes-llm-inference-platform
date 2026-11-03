"""Cross-Pod visibility must not discard the original timeline or restart execution."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from platform_contracts import RunnerObservation, RunResult

pytest.importorskip("platform_management")
from platform_management.integrations.job_runner import KubernetesJobRunner  # noqa: E402


@pytest.mark.parametrize("stale", ["absent", "queued", "different-result", "missing-origin"])
def test_completed_job_waits_for_consistent_observation_then_keeps_origin(repo, stale):
    result = RunResult.model_validate_json(
        json.dumps(
            json.loads(
                (repo / "contracts/fixtures/runs/writer-assist/completed-passing.json").read_text()
            )["result"]
        )
    )
    runner = object.__new__(KubernetesJobRunner)
    runner.binding = {
        "runner": "kubernetes-workload-stub:v1",
        "finalization_allowance_seconds": 120,
    }
    runner.name = Mock(return_value="original-job")
    runner.client = Mock()
    job = {
        "status": {
            "conditions": [{"type": "Complete", "status": "True"}],
            "completionTime": datetime.now(UTC).isoformat(),
        }
    }
    runner.client.job.return_value = job
    runner.client.pods.return_value = []
    runner.validate_job = Mock(return_value=job)
    runner.validated_result = Mock(return_value=result)
    origin = datetime.now(UTC) - timedelta(seconds=5)
    final = RunnerObservation(status="completed", result=result, timeline_origin_at=origin)
    previous = {
        "absent": None,
        "queued": RunnerObservation(status="queued"),
        "different-result": final.model_copy(
            update={"result": result.model_copy(update={"notes": "old"})}
        ),
        "missing-origin": final.model_copy(update={"timeline_origin_at": None}),
    }[stale]
    runner.saved_observation = Mock(side_effect=[previous, final, previous])
    first = runner.observe("op-original", "original-uid")
    assert first.status == "running" and first.stage == "finalizing"
    observed = runner.observe("op-original", "original-uid")
    assert observed.status == "completed" and observed.timeline_origin_at == origin
    assert observed.result == result
    # A replacement observer cannot reset the shared-storage consistency deadline.
    job["status"]["completionTime"] = (datetime.now(UTC) - timedelta(seconds=121)).isoformat()
    expired = runner.observe("op-original", "original-uid")
    assert expired.status == "failed" and expired.error.code == "invalid_result"
    runner.client.request.assert_not_called()


def test_missing_result_is_bounded_by_original_job_completion():
    runner = object.__new__(KubernetesJobRunner)
    runner.binding = {"runner": "kubernetes:v1"}
    runner.name = Mock(return_value="original-job")
    runner.client = Mock()
    job = {
        "status": {
            "conditions": [{"type": "Complete", "status": "True"}],
            "completionTime": datetime.now(UTC).isoformat(),
        }
    }
    runner.client.job.return_value = job
    runner.client.pods.return_value = []
    runner.validate_job = Mock(return_value=job)
    runner.validated_result = Mock(side_effect=FileNotFoundError())
    assert runner.observe("op-original", "original-uid").stage == "finalizing"
    job["status"]["completionTime"] = (datetime.now(UTC) - timedelta(seconds=121)).isoformat()
    assert runner.observe("op-original", "original-uid").error.code == "invalid_result"
    runner.client.request.assert_not_called()
