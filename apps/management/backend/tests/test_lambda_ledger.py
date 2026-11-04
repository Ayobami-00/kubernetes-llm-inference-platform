"""Real PostgreSQL coordination with mocked provider I/O, never a paid rental."""

import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import Mock

import pytest
from platform_contracts import HostTask, HostTaskObservation
from platform_management.db.models import ProviderIntent
from platform_management.db.session import session
from platform_management.providers.base import AllocationAmbiguous, CapacityUnavailable
from platform_management.providers.lambda_ledger import (
    PostgresLaunchLedger,
    allocate_once,
    terminate_once,
)
from platform_management.providers.lambda_mutations import (
    LaunchPlan,
    ProviderIdentityConflict,
    ProviderRateLimited,
    ProviderRejected,
)
from platform_management.services.common import digest, now
from sqlalchemy import select

USER_DATA = "#cloud-config\nruncmd: []\n"
INSTANCE = "a" * 32


def plan(identity="1"):
    return LaunchPlan(
        intent_id="op-" + identity * 16,
        instance_type="gpu_1x_a10",
        region="us-east-1",
        image_id="synthetic-image",
        ssh_key_name="synthetic-executor-key",
        firewall_ruleset_ids=("synthetic-firewall",),
        user_data_sha256=hashlib.sha256(USER_DATA.encode()).hexdigest(),
    )


def intent(document):
    with session() as db, db.begin():
        db.add(
            ProviderIntent(
                id=document.intent_id, group_id="test-group", payload={"desired": 1, "mode": "real"}
            )
        )


def observation(document):
    return {
        "id": INSTANCE,
        "name": document.name,
        "tags": [{"key": k, "value": v} for k, v in document.tags.items()],
        "instance_type": {"name": document.instance_type},
        "region": {"name": document.region},
        "ssh_key_names": [document.ssh_key_name],
        "status": "active",
    }


def test_dispatch_is_committed_before_http_and_terminal_key_is_never_relaunched():
    document, ledger = plan(), PostgresLaunchLedger()
    intent(document)
    api = Mock()
    api.instances.return_value = []

    def post(*_):
        # A separate database session sees the committed boundary before I/O.
        assert ledger.read(document)["state"] == "dispatching"
        return (INSTANCE,)

    api.launch.side_effect = post
    assert allocate_once(document, USER_DATA, ledger, api) == (INSTANCE,)
    assert ledger.read(document)["instance_ids"] == [INSTANCE]
    api.instances.return_value = [observation(document)]
    assert allocate_once(document, USER_DATA, ledger, api) == (INSTANCE,)
    api.launch.assert_called_once()


def test_timeout_and_empty_list_never_authorize_second_post():
    document, ledger = plan(), PostgresLaunchLedger()
    intent(document)
    api = Mock()
    api.instances.return_value = []
    api.launch.side_effect = AllocationAmbiguous("Synthetic lost response after commit")
    with pytest.raises(AllocationAmbiguous):
        allocate_once(document, USER_DATA, ledger, api)
    with pytest.raises(AllocationAmbiguous):
        allocate_once(document, USER_DATA, ledger, api)
    assert ledger.read(document)["state"] == "dispatching"
    api.launch.assert_called_once()
    # Later provider visibility adopts the original allocation instead of retrying.
    api.instances.return_value = [observation(document)]
    assert allocate_once(document, USER_DATA, ledger, api) == (INSTANCE,)
    api.launch.assert_called_once()


def test_crash_before_post_keeps_reservation_and_conflicting_plan_is_rejected():
    document, ledger = plan(), PostgresLaunchLedger()
    intent(document)
    ledger.freeze(document)
    assert ledger.dispatch(document)
    with pytest.raises(ProviderIdentityConflict):
        ledger.freeze(document.model_copy(update={"image_id": "changed-image"}))
    api = Mock()
    api.instances.return_value = []
    with pytest.raises(AllocationAmbiguous):
        allocate_once(document, USER_DATA, ledger, api)
    api.launch.assert_not_called()


def test_concurrent_dispatch_and_different_group_share_one_allocation_envelope():
    first, second, ledger = plan("1"), plan("2"), PostgresLaunchLedger()
    for document in (first, second):
        intent(document)
        ledger.freeze(document)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ledger.dispatch(first), range(2)))
    assert sorted(results) == [False, True]
    with pytest.raises(CapacityUnavailable, match="reservation"):
        ledger.dispatch(second)
    with session() as db:
        rows = list(db.scalars(select(ProviderIntent)))
        assert sum(row.state == "dispatching" for row in rows) == 1


@pytest.mark.parametrize(
    "error,reason",
    [
        (CapacityUnavailable("Synthetic shortage"), "capacity_unavailable"),
        (ProviderRejected("Synthetic definite rejection"), "request_rejected"),
        (ProviderRateLimited("Synthetic rate limit"), "rate_limited"),
    ],
)
def test_definite_rejection_is_terminal_and_releases_slot(error, reason):
    first, second, ledger = plan("1"), plan("2"), PostgresLaunchLedger()
    intent(first)
    api = Mock()
    api.instances.return_value = []
    api.launch.side_effect = error
    with pytest.raises(type(error)):
        allocate_once(first, USER_DATA, ledger, api)
    assert ledger.read(first)["rejection"] == reason
    with pytest.raises(ProviderRejected):
        allocate_once(first, USER_DATA, ledger, api)
    api.launch.assert_called_once()
    intent(second)
    ledger.freeze(second)
    assert ledger.dispatch(second)


def test_orphan_project_instance_blocks_new_rental_without_touching_it():
    document, ledger = plan(), PostgresLaunchLedger()
    intent(document)
    api = Mock()
    api.instances.return_value = [
        {
            "id": "b" * 32,
            "name": "klip-unreconciled",
            "status": "active",
            "tags": [{"key": "klip-cluster", "value": "klip"}],
        }
    ]
    with pytest.raises(CapacityUnavailable, match="reconciled"):
        allocate_once(document, USER_DATA, ledger, api)
    assert ledger.read(document)["state"] == "pending"
    api.launch.assert_not_called()


def test_first_stage_rejects_more_expensive_or_different_region_capacity():
    document = plan().model_copy(update={"instance_type": "gpu_8x_a100"})
    ledger = PostgresLaunchLedger()
    intent(document)
    ledger.freeze(document)
    with pytest.raises(CapacityUnavailable, match="A10"):
        ledger.dispatch(document)
    assert ledger.read(document)["state"] == "pending"


def test_simulation_intent_cannot_be_converted_to_paid_allocation():
    document = plan()
    with session() as db, db.begin():
        db.add(ProviderIntent(id=document.intent_id, group_id="simulation", payload={"desired": 1}))
    with pytest.raises(ValueError, match="simulated"):
        PostgresLaunchLedger().freeze(document)


def test_account_gate_persists_pacing_and_does_not_hold_a_transaction():
    import time

    from platform_management.db.models import Preference
    from platform_management.providers.lambda_ledger import request_gate

    starts = []

    def request(_):
        with request_gate():
            starts.append(time.monotonic())
            with session() as db:
                assert db.get(Preference, "lambda-api-pacing-v1").payload["next_request_at"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(request, range(2)))
    assert sorted(starts)[1] - sorted(starts)[0] >= 1.0


def cluster_metadata(**changes):
    return {
        "schema_version": "klip-host-executor-metadata.v1",
        "cluster_id": "klip",
        "kube_system_uid": "12345678-1234-1234-1234-123456789abc",
        "registry_revision": 12,
        "occupied_wireguard_ips": [f"10.200.0.{i}" for i in (11, 12, 13, 64, 65, 66)],
        **changes,
    }


def allocated_ledger(reserve=True):
    document, ledger = plan(), PostgresLaunchLedger()
    intent(document)
    ledger.freeze(document)
    assert ledger.dispatch(document)
    ledger.allocated(document, [INSTANCE])
    if reserve:
        ledger.reserve_wireguard(document, cluster_metadata())
    return document, ledger


def host_receipt(action="cleanup-check", *, fence=None):
    task = HostTask(
        id="host-" + ("2" if action == "revoke-bootstrap" else "1") * 24,
        action=action,
        provider_instance_id=INSTANCE,
        node_name="gpu-" + INSTANCE,
        public_ip="8.8.8.8",
        private_ip="192.168.1.2",
        wireguard_ip="10.200.0.67",
        trusted_host_key="ssh-ed25519 "
        + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"x" * 32).decode(),
        registry_revision=12,
        deadline=now() + timedelta(minutes=10),
        previous_task_id="host-" + "0" * 24,
        provider_fence=fence,
        node_uid="12345678-1234-1234-1234-123456789abc" if action == "remove" else None,
    )
    inputs = task.model_dump(mode="json")
    if inputs.get("provider_fence") is None:
        inputs.pop("provider_fence", None)
    result = HostTaskObservation(
        task_id=task.id,
        input_sha256=digest(inputs),
        job_uid="test-job-uid",
        status="completed",
        stage="Synthetic safety evidence",
        observed_at=now(),
        registry_revision=12,
        evidence_sha256="a" * 64,
        bootstrap_fence_permitted=action == "cleanup-check",
        bootstrap_cleanup_complete=action == "revoke-bootstrap",
        node_name=task.node_name if task.node_uid else None,
        node_uid=task.node_uid,
    )
    return task, result


def remember_host_receipt(document, task, result):
    """Seed a retained executor receipt; task-dispatch behavior has separate tests."""
    with session() as db, db.begin():
        row = db.get(ProviderIntent, document.intent_id, with_for_update=True)
        value = row.payload["lambda_launch"]
        tasks = {
            **value["host_tasks"],
            task.id: {
                "task": task.model_dump(mode="json"),
                "input_sha256": result.input_sha256,
                "observation": result.model_dump(mode="json"),
            },
        }
        row.payload = {
            **row.payload,
            "lambda_launch": {**value, "host_tasks": tasks, "active_host_task_id": task.id},
        }


def test_termination_requires_host_authorization_before_any_http():
    document, ledger = allocated_ledger()
    api = Mock()
    with pytest.raises(ProviderIdentityConflict, match="safety evidence"):
        terminate_once(document, ledger, api)
    assert not api.mock_calls and ledger.read(document)["state"] == "allocated"


def test_fenced_allocation_holds_slot_until_exact_executor_cleanup():
    document, ledger = allocated_ledger()
    check, checked = host_receipt()
    remember_host_receipt(document, check, checked)
    ledger.authorize_termination(document, check, checked)
    api = Mock()
    observed = observation(document)
    api.instances.return_value, api.instance.return_value = [observed], observed

    def terminate(_):
        assert ledger.read(document)["state"] == "terminating"
        return [{**observed, "status": "terminating"}]

    api.terminate.side_effect = terminate
    assert terminate_once(document, ledger, api) is None
    api.instances.return_value, api.instance.return_value = [], None
    fence = terminate_once(document, ledger, api)
    assert ledger.read(document)["state"] == "fenced"
    second = plan("2")
    intent(second)
    ledger.freeze(second)
    with pytest.raises(CapacityUnavailable, match="reservation"):
        ledger.dispatch(second)
    revoke, revoked = host_receipt("revoke-bootstrap", fence=fence)
    remember_host_receipt(document, revoke, revoked)
    with pytest.raises(ProviderIdentityConflict):
        ledger.cleanup_completed(document, revoke, checked)
    assert ledger.read(document)["state"] == "fenced"
    ledger.cleanup_completed(document, revoke, revoked)
    ledger.cleanup_completed(document, revoke, revoked)
    assert ledger.read(document)["state"] == "terminated" and ledger.dispatch(second)
    api.reset_mock()
    assert terminate_once(document, ledger, api) == fence
    # A historical launch is returned even when bootstrap material is gone.
    assert allocate_once(document, None, ledger, api) == (INSTANCE,)
    assert not api.mock_calls


def test_drained_removal_is_completed_only_after_provider_fencing():
    document, ledger = allocated_ledger()
    task, result = host_receipt("remove")
    remember_host_receipt(document, task, result)
    ledger.authorize_termination(document, task, result)
    with pytest.raises(ProviderIdentityConflict, match="not complete"):
        ledger.cleanup_completed(document, task, result)
    api = Mock()
    observed = {**observation(document), "status": "terminated"}
    api.instances.return_value, api.instance.return_value = [observed], observed
    assert terminate_once(document, ledger, api).provider_instance_id == INSTANCE
    ledger.cleanup_completed(document, task, result)
    assert ledger.read(document)["state"] == "terminated"
    api.terminate.assert_not_called()


def test_termination_authorization_rejects_unrelated_and_incomplete_receipts():
    document, ledger = allocated_ledger()
    task, result = host_receipt()
    remember_host_receipt(document, task, result)
    for changed in (
        result.model_copy(update={"input_sha256": "b" * 64}),
        result.model_copy(update={"task_id": "host-" + "f" * 24}),
        result.model_copy(update={"cleanup_pending": True}),
        result.model_copy(update={"bootstrap_fence_permitted": False}),
    ):
        with pytest.raises(ProviderIdentityConflict):
            ledger.authorize_termination(document, task, changed)
    ledger.authorize_termination(document, task, result)
    ledger.authorize_termination(document, task, result)
    with pytest.raises(ProviderIdentityConflict):
        ledger.authorize_termination(
            document, task, result.model_copy(update={"evidence_sha256": "b" * 64})
        )


def test_allocated_replay_needs_no_provider_or_bootstrap_readiness():
    document, ledger = allocated_ledger()
    api = Mock()
    assert allocate_once(document, None, ledger, api) == (INSTANCE,)
    assert not api.mock_calls
    with pytest.raises(ProviderIdentityConflict):
        allocate_once(document.model_copy(update={"image_id": "changed"}), None, ledger, api)


def test_address_reservation_is_durable_concurrent_and_skips_executor_peers():
    document, ledger = allocated_ledger(reserve=False)
    metadata = cluster_metadata()
    metadata["occupied_wireguard_ips"].append("10.200.0.67")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ledger.reserve_wireguard(document, metadata), range(2)))
    assert results == ["10.200.0.68", "10.200.0.68"]
    assert ledger.read(document)["wireguard_reservation"]["registry_revision"] == 12
    with pytest.raises(ProviderIdentityConflict, match="cluster identity"):
        ledger.reserve_wireguard(
            document, cluster_metadata(kube_system_uid="abcdefab-1234-1234-1234-123456789abc")
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "other"},
        {"cluster_id": "other"},
        {"registry_revision": True},
        {"occupied_wireguard_ips": ["10.200.0.11"]},
        {
            "occupied_wireguard_ips": [f"10.200.0.{i}" for i in (11, 12, 13, 64, 65, 66)]
            + ["169.254.169.254"]
        },
    ],
)
def test_address_reservation_rejects_malformed_executor_scope(changes):
    document, ledger = allocated_ledger(reserve=False)
    with pytest.raises(ProviderIdentityConflict):
        ledger.reserve_wireguard(document, cluster_metadata(**changes))
