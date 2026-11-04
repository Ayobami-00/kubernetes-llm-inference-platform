"""Real PostgreSQL ownership with a mocked authenticated executor; no host mutation."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from platform_contracts import HostTask, HostTaskObservation
from platform_management.integrations.host_tasks import DurableHostTasks, task_digest
from platform_management.providers.base import TransientProviderError
from platform_management.providers.lambda_ledger import terminate_once
from platform_management.providers.lambda_mutations import ProviderIdentityConflict
from platform_management.services.common import now
from test_lambda_ledger import INSTANCE, allocated_ledger, host_receipt


def prepared_task():
    base, _ = host_receipt()
    return HostTask.model_validate(
        {**base.model_dump(mode="json"), "action": "prepare", "previous_task_id": None}
    )


def observation(task, status="running", **changes):
    return HostTaskObservation(
        task_id=task.id,
        input_sha256=task_digest(task),
        job_uid="native-test-job",
        status=status,
        stage="Synthetic executor observation",
        observed_at=now(),
        registry_revision=task.registry_revision,
        **changes,
    )


def test_task_is_durable_before_io_and_terminal_reuse_needs_no_credentials():
    plan, ledger = allocated_ledger()
    task, client = prepared_task(), Mock()
    store = DurableHostTasks(plan, lambda: client)

    def launch(supplied):
        assert supplied == task
        receipt = ledger.read(plan)["host_tasks"][task.id]
        assert receipt["task"] == task.model_dump(mode="json") and receipt["observation"] is None
        return observation(task)

    client.launch.side_effect = launch
    assert store.launch(task).status == "running"
    assert client.close.call_count == 1
    complete = observation(task, "completed", host_ready=True, evidence_sha256="a" * 64)
    client.observe.return_value = complete
    assert DurableHostTasks(plan, lambda: client).observe(task.id) == complete
    closed = Mock(side_effect=AssertionError("Terminal history must not load TLS credentials"))
    resumed = DurableHostTasks(plan, closed)
    assert resumed.launch(task) == complete and resumed.observe(task.id) == complete
    closed.assert_not_called()
    with pytest.raises(ProviderIdentityConflict, match="inputs changed"):
        resumed.launch(task.model_copy(update={"drain_timeout_s": 60}))


def test_lost_transport_reuses_the_frozen_task_instead_of_building_a_replacement():
    plan, ledger = allocated_ledger()
    task, client = prepared_task(), Mock()
    client.launch.side_effect = TransientProviderError("Synthetic interrupted response")
    store = DurableHostTasks(plan, lambda: client)
    with pytest.raises(TransientProviderError):
        store.launch(task)
    assert list(ledger.read(plan)["host_tasks"]) == [task.id]
    client.launch.side_effect = None
    client.launch.return_value = observation(task)
    assert DurableHostTasks(plan, lambda: client).launch(task).status == "running"
    assert all(c.args == (task,) for c in client.launch.call_args_list)


def test_failed_owner_is_terminal_and_only_cleanup_can_follow():
    plan, _ = allocated_ledger()
    task, client = prepared_task(), Mock()
    client.launch.return_value = observation(task)
    store = DurableHostTasks(plan, lambda: client)
    store.launch(task)
    lost = observation(
        task,
        "failed",
        cleanup_pending=True,
        error={"code": "host_job_lost", "message": "Synthetic lost owner"},
    )
    client.observe.return_value = lost
    assert store.observe(task.id) == lost
    join = HostTask.model_validate(
        {
            **task.model_dump(mode="json"),
            "id": "host-" + "2" * 24,
            "action": "join",
            "previous_task_id": task.id,
        }
    )
    with pytest.raises(ProviderIdentityConflict, match="task chain"):
        store.freeze(join)
    cleanup = join.model_copy(update={"action": "cleanup-check"})
    store.freeze(cleanup)
    assert store.read(cleanup.id)["observation"] is None


@pytest.mark.parametrize("changed", ["job", "sha", "node", "revision"])
def test_observation_drift_is_rejected(changed):
    plan, _ = allocated_ledger()
    task = prepared_task()
    store = DurableHostTasks(plan, Mock())
    store.freeze(task)
    store.record(observation(task))
    result = observation(task)
    updates = {
        "job": {"job_uid": "replacement-job"},
        "sha": {"input_sha256": "b" * 64},
        "node": {"node_name": "another-node", "node_uid": "another-uid"},
        "revision": {"registry_revision": 0},
    }
    with pytest.raises(ProviderIdentityConflict):
        store.record(result.model_copy(update=updates[changed]))


def test_concurrent_initial_tasks_admit_only_one_chain():
    plan, ledger = allocated_ledger()
    first = prepared_task()
    second = first.model_copy(update={"id": "host-" + "2" * 24})

    def freeze(task):
        try:
            DurableHostTasks(plan, Mock()).freeze(task)
            return True
        except ProviderIdentityConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(freeze, [first, second])) == [False, True]
    assert len(ledger.read(plan)["host_tasks"]) == 1


def test_no_host_dispatch_proof_allows_failed_attestation_cleanup_without_ssh():
    plan, ledger = allocated_ledger()
    ledger.authorize_unbootstrapped(plan)
    ledger.authorize_unbootstrapped(plan)
    with pytest.raises(ProviderIdentityConflict):
        DurableHostTasks(plan, Mock()).freeze(prepared_task())
    with pytest.raises(ProviderIdentityConflict):
        ledger.unbootstrapped_cleanup_completed(plan)
    api = Mock()
    api.instances.return_value, api.instance.return_value = [], None
    assert terminate_once(plan, ledger, api).provider_instance_id == INSTANCE
    ledger.unbootstrapped_cleanup_completed(plan)
    ledger.unbootstrapped_cleanup_completed(plan)
    assert ledger.read(plan)["state"] == "terminated"
    api.terminate.assert_not_called()


def test_dispatched_host_or_historical_missing_field_cannot_claim_no_bootstrap():
    from platform_management.db.models import ProviderIntent
    from platform_management.db.session import session

    plan, ledger = allocated_ledger()
    task = prepared_task()
    store = DurableHostTasks(plan, Mock())
    store.freeze(task)
    with pytest.raises(ProviderIdentityConflict, match="absence"):
        ledger.authorize_unbootstrapped(plan)
    with session() as db, db.begin():
        row = db.get(ProviderIntent, plan.intent_id, with_for_update=True)
        value = dict(row.payload["lambda_launch"])
        value.pop("host_tasks")
        value.pop("active_host_task_id")
        row.payload = {**row.payload, "lambda_launch": value}
    with pytest.raises(ProviderIdentityConflict, match="absence"):
        ledger.authorize_unbootstrapped(plan)


def test_unbootstrapped_authorization_and_first_host_dispatch_are_serialized():
    plan, ledger = allocated_ledger()

    def compete(action):
        try:
            if action == "authorize":
                ledger.authorize_unbootstrapped(plan)
            else:
                DurableHostTasks(plan, Mock()).freeze(prepared_task())
            return True
        except ProviderIdentityConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(compete, ["authorize", "dispatch"])) == [False, True]
