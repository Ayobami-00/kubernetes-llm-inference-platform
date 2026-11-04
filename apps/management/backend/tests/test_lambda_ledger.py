"""Real PostgreSQL coordination with mocked provider I/O, never a paid rental."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from platform_management.db.models import ProviderIntent
from platform_management.db.session import session
from platform_management.providers.base import AllocationAmbiguous, CapacityUnavailable
from platform_management.providers.lambda_ledger import PostgresLaunchLedger, allocate_once
from platform_management.providers.lambda_mutations import (
    LaunchPlan,
    ProviderIdentityConflict,
    ProviderRateLimited,
    ProviderRejected,
)
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
