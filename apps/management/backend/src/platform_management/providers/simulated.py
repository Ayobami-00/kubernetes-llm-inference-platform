"""Persistent provider ledger, deliberately committed independently of job checkpoints."""

from sqlalchemy import select

from platform_management.db.models import ProviderIntent, SimInstance
from platform_management.db.session import session
from platform_management.providers.base import (
    AllocationAmbiguous,
    CapacityUnavailable,
    TransientProviderError,
)
from platform_management.services.common import now


class SimulatedProvider:
    def reconcile(self, intent_id):
        with session() as db:
            intent = db.get(ProviderIntent, intent_id)
            if intent.state == "unknown":
                return None
            if intent.state == "pending":
                return []  # The transactional simulated ledger confirms absence.
            return [
                {"id": r.id, "allocated_at": r.allocated_at.isoformat(), "state": r.state}
                for r in db.scalars(select(SimInstance).where(SimInstance.intent_id == intent_id))
            ]

    def allocate(self, intent_id):
        ambiguous = False
        with session() as db, db.begin():
            intent = db.get(ProviderIntent, intent_id, with_for_update=True)
            if intent.state != "pending":
                existing = list(
                    db.scalars(select(SimInstance).where(SimInstance.intent_id == intent_id))
                )
                return [
                    {"id": r.id, "allocated_at": r.allocated_at.isoformat(), "state": r.state}
                    for r in existing
                ]
            fault = intent.payload["fault"]
            if fault == "shortage":
                raise CapacityUnavailable("Synthetic capacity shortage")
            if fault == "transient" and not intent.payload.get("injected"):
                intent.payload = {**intent.payload, "injected": True}
                # Commit the injected fault before returning it to the caller.
                transient = True
            else:
                transient = False
                desired = intent.payload["desired"]
                count = max(1, desired - 1) if fault == "partial" else desired
                stamp = now()
                for index in range(count):
                    db.add(
                        SimInstance(
                            id=f"sim-{intent_id}-{index}",
                            intent_id=intent_id,
                            group_id=intent.group_id,
                            state="allocated",
                            allocated_at=stamp,
                        )
                    )
                intent.state = "allocated"
                ambiguous = fault == "ambiguous"
        if transient:
            raise TransientProviderError("Synthetic temporary provider failure")
        if ambiguous:
            raise AllocationAmbiguous("Synthetic timeout after provider committed allocation")
        return self.reconcile(intent_id)

    def terminate(self, instance_id, fault):
        if fault == "termination":
            raise TransientProviderError("Synthetic termination failure; cleanup remains pending")
        with session() as db, db.begin():
            row = db.get(SimInstance, instance_id, with_for_update=True)
            if row:
                row.state = "terminated"

    def termination_confirmed(self, instance_id):
        with session() as db:
            row = db.get(SimInstance, instance_id)
            return row is not None and row.state == "terminated"
