"""Durable launch ownership and account-wide API pacing without long transactions."""

import copy
import time
from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy import select, text

from platform_management.db.models import Preference, ProviderIntent
from platform_management.db.session import engine, session
from platform_management.providers.base import AllocationAmbiguous, CapacityUnavailable
from platform_management.providers.lambda_mutations import (
    LaunchPlan,
    ProviderIdentityConflict,
    ProviderRateLimited,
    ProviderRejected,
    reconcile_launch,
)
from platform_management.services.common import digest, lock_key, now


@contextmanager
def request_gate(*, launch=False):
    """All Lambda consumers must use this same lock, including capacity refresh.

    Hold a session advisory lock across the bounded HTTP request, without an open
    transaction. Record the next start before I/O. A process loss releases the
    lock but retains pacing. Database failure prevents the provider request.
    """
    key = lock_key("lambda-api-account-v1")
    with engine().connect().execution_options(isolation_level="AUTOCOMMIT") as guard:
        guard.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
        try:
            with session() as db:
                row = db.get(Preference, "lambda-api-pacing-v1")
                previous = row.payload if row else {}
            from datetime import datetime

            due = max(
                [now()]
                + [
                    datetime.fromisoformat(previous[field])
                    for field in ("next_request_at", "next_launch_at")
                    if previous.get(field) and (field != "next_launch_at" or launch)
                ]
            )
            wait = (due - now()).total_seconds()
            if wait > 0:
                time.sleep(wait)
            stamp = now()
            values = {**previous, "next_request_at": (stamp + timedelta(seconds=1.05)).isoformat()}
            if launch:
                values["next_launch_at"] = (stamp + timedelta(seconds=12.1)).isoformat()
            with session() as db, db.begin():
                db.merge(Preference(id="lambda-api-pacing-v1", payload=values))
            yield
        finally:
            guard.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


class PostgresLaunchLedger:
    """Uses C's existing intent table; no private bootstrap material is stored here."""

    @staticmethod
    def checked(row, plan):
        value = row.payload.get("lambda_launch") if row else None
        if not value or value.get("plan_sha256") != digest(plan.model_dump(mode="json")):
            raise ProviderIdentityConflict("Durable Lambda plan is absent or changed")
        return value

    def freeze(self, plan: LaunchPlan):
        with session() as db, db.begin():
            row = db.get(ProviderIntent, plan.intent_id, with_for_update=True)
            if not row:
                raise ValueError("The owning operation must commit its provider intent first")
            if row.payload.get("mode") != "real":
                raise ValueError("A simulated intent cannot dispatch a real Lambda allocation")
            if "lambda_launch" in row.payload:
                self.checked(row, plan)
                return
            if row.state != "pending":
                raise ProviderIdentityConflict("Cannot replace an attempted provider intent")
            row.payload = {
                **row.payload,
                "lambda_launch": {
                    "plan": plan.model_dump(mode="json"),
                    "plan_sha256": digest(plan.model_dump(mode="json")),
                    "state": "pending",
                    "instance_ids": [],
                },
            }

    def read(self, plan):
        with session() as db:
            return copy.deepcopy(self.checked(db.get(ProviderIntent, plan.intent_id), plan))

    def dispatch(self, plan):
        with session() as db, db.begin():
            db.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": lock_key("lambda-allocation-envelope:klip")},
            )
            row = db.get(ProviderIntent, plan.intent_id, with_for_update=True)
            value = self.checked(row, plan)
            if value["state"] != "pending":
                return False
            # The first gate permits one A10. Expansion needs a new reviewed policy.
            if plan.instance_type != "gpu_1x_a10" or plan.region != "us-east-1":
                raise CapacityUnavailable(
                    "The current allocation envelope permits one Virginia A10"
                )
            for other in db.scalars(select(ProviderIntent)):
                allocation = other.payload.get("lambda_launch")
                if (
                    other.id != row.id
                    and allocation
                    and allocation["state"]
                    not in {
                        "pending",
                        "rejected",
                        "terminated",
                    }
                ):
                    raise CapacityUnavailable("Another durable GPU reservation remains unresolved")
            stamp = now()
            row.state = "dispatching"
            row.payload = {
                **row.payload,
                "lambda_launch": {
                    **value,
                    "state": "dispatching",
                    "dispatched_at": stamp.isoformat(),
                    "expires_at": (stamp + timedelta(minutes=plan.lease_minutes)).isoformat(),
                },
            }
            return True

    def allocated(self, plan, identities):
        if len(identities) != 1 or len(set(identities)) != 1:
            raise ProviderIdentityConflict("Exactly one provider allocation must be recorded")
        with session() as db, db.begin():
            row = db.get(ProviderIntent, plan.intent_id, with_for_update=True)
            value = self.checked(row, plan)
            if value["instance_ids"] and value["instance_ids"] != list(identities):
                raise ProviderIdentityConflict("Allocation receipt conflicts with durable identity")
            if value["state"] not in {"dispatching", "allocated"}:
                raise ProviderIdentityConflict("Allocation receipt has no committed dispatch")
            row.state = "allocated"
            row.payload = {
                **row.payload,
                "lambda_launch": {**value, "state": "allocated", "instance_ids": list(identities)},
            }

    def rejected(self, plan, code):
        if code not in {"capacity_unavailable", "request_rejected", "rate_limited"}:
            raise ValueError("Only a definite API rejection can release a reservation")
        with session() as db, db.begin():
            row = db.get(ProviderIntent, plan.intent_id, with_for_update=True)
            value = self.checked(row, plan)
            if value["state"] != "dispatching" or value["instance_ids"]:
                raise ProviderIdentityConflict("Cannot reject an identified allocation")
            row.state = "rejected"
            row.payload = {
                **row.payload,
                "lambda_launch": {**value, "state": "rejected", "rejection": code},
            }


def allocate_once(plan, user_data, ledger, api):
    """Crash-safe launch coordinator. Repeated calls reconcile the original intent."""
    plan.request(user_data)  # Check secret-file digest before reserving capacity.
    ledger.freeze(plan)
    current = ledger.read(plan)
    instances = api.instances()
    observed = reconcile_launch(current["state"], plan, instances, current["instance_ids"])
    if observed:
        ledger.allocated(plan, [i["id"] for i in observed])
        return tuple(i["id"] for i in observed)
    if current["state"] == "rejected":
        raise ProviderRejected("The original allocation was rejected; create a new operation")
    for instance in instances:
        if {"key": "klip-cluster", "value": plan.cluster_id} in instance.get(
            "tags", []
        ) and instance.get("status") not in {"terminated", "preempted"}:
            raise CapacityUnavailable("Existing project GPU capacity must be reconciled first")
    if observed is None or not ledger.dispatch(plan):
        raise AllocationAmbiguous(
            "Original allocation remains unresolved; no second POST permitted"
        )
    # No HTTP request can occur before the dispatch transaction has committed.
    try:
        identities = api.launch(plan, user_data)
    except CapacityUnavailable:
        ledger.rejected(plan, "capacity_unavailable")
        raise
    except ProviderRejected:
        ledger.rejected(plan, "request_rejected")
        raise
    except ProviderRateLimited:
        ledger.rejected(plan, "rate_limited")
        raise
    # Ambiguous failures deliberately leave the reservation in dispatching.
    ledger.allocated(plan, identities)
    return identities
