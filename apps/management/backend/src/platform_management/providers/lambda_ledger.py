"""Durable launch ownership and account-wide API pacing without long transactions."""

import copy
import re
import time
from contextlib import contextmanager
from datetime import timedelta
from ipaddress import IPv4Address, IPv4Network

from platform_contracts import HostTask, HostTaskObservation, ProviderFence
from sqlalchemy import select, text

from platform_management.db.models import Preference, ProviderIntent
from platform_management.db.session import engine, session
from platform_management.providers.base import AllocationAmbiguous, CapacityUnavailable
from platform_management.providers.lambda_mutations import (
    LaunchPlan,
    ProviderIdentityConflict,
    ProviderRateLimited,
    ProviderRejected,
    owned_instances,
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
                    "host_tasks": {},
                    "active_host_task_id": None,
                },
            }

    def read(self, plan):
        with session() as db:
            return copy.deepcopy(self.checked(db.get(ProviderIntent, plan.intent_id), plan))

    def record_preflight(self, plan, evidence):
        with session() as db, db.begin():
            row = db.get(ProviderIntent, plan.intent_id, with_for_update=True)
            value = self.checked(row, plan)
            if value["state"] != "pending":
                return
            row.payload = {
                **row.payload,
                "lambda_launch": {
                    **value,
                    "allocation_preflight": {**evidence, "observed_at": now().isoformat()},
                },
            }

    def abandon_pending(self, plan):
        with session() as db, db.begin():
            row = db.get(ProviderIntent, plan.intent_id, with_for_update=True)
            value = self.checked(row, plan)
            if value["state"] == "rejected" and value.get("rejection") == "not_dispatched":
                return
            if value["state"] != "pending" or value["instance_ids"]:
                raise ProviderIdentityConflict(
                    "An attempted launch cannot be abandoned as unsubmitted"
                )
            row.state = "rejected"
            row.payload = {
                **row.payload,
                "lambda_launch": {**value, "state": "rejected", "rejection": "not_dispatched"},
            }

    def renew(self, plan, operation_key, additional_minutes, maximum_total_minutes):
        with session() as db, db.begin():
            row = db.get(ProviderIntent, plan.intent_id, with_for_update=True)
            value = self.checked(row, plan)
            renewals = value.get("renewals", {})
            if operation_key in renewals:
                if renewals[operation_key]["additional_minutes"] != additional_minutes:
                    raise ProviderIdentityConflict("Lease renewal key has conflicting inputs")
                return renewals[operation_key]["expires_at"]
            from datetime import datetime

            if (
                value["state"] != "allocated"
                or value.get("termination_authorization")
                or type(additional_minutes) is not int
                or not 0 < additional_minutes <= 90
                or datetime.fromisoformat(value["expires_at"]) <= now()
            ):
                raise CapacityUnavailable("The real lease cannot be renewed")
            expiry = datetime.fromisoformat(value["expires_at"]) + timedelta(
                minutes=additional_minutes
            )
            if expiry > datetime.fromisoformat(value["dispatched_at"]) + timedelta(
                minutes=maximum_total_minutes
            ):
                raise CapacityUnavailable("Renewal exceeds the reviewed maximum total lease")
            receipt = {
                "additional_minutes": additional_minutes,
                "expires_at": expiry.isoformat(),
                "at": now().isoformat(),
            }
            row.payload = {
                **row.payload,
                "lambda_launch": {
                    **value,
                    "expires_at": expiry.isoformat(),
                    "renewals": {**renewals, operation_key: receipt},
                },
            }
            return expiry.isoformat()

    def reserve_wireguard(self, plan, metadata):
        """Reserve from fresh authenticated executor metadata, retaining failed leases."""
        if (
            metadata.get("schema_version") != "klip-host-executor-metadata.v1"
            or metadata.get("cluster_id") != plan.cluster_id
            or not re.fullmatch(
                r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", metadata.get("kube_system_uid", "")
            )
            or type(metadata.get("registry_revision")) is not int
            or metadata["registry_revision"] < 1
            or not isinstance(metadata.get("occupied_wireguard_ips"), list)
        ):
            raise ProviderIdentityConflict("Executor cluster metadata is malformed")
        occupied = {str(IPv4Address(address)) for address in metadata["occupied_wireguard_ips"]}
        if not {
            "10.200.0.11",
            "10.200.0.12",
            "10.200.0.13",
            "10.200.0.64",
            "10.200.0.65",
            "10.200.0.66",
        } <= occupied or any(
            IPv4Address(address) not in IPv4Network("10.200.0.0/24") for address in occupied
        ):
            raise ProviderIdentityConflict("Executor fixed CPU address assignments differ")
        with session() as db, db.begin():
            db.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": lock_key("lambda-wireguard-reservations:klip")},
            )
            row = db.get(ProviderIntent, plan.intent_id, with_for_update=True)
            value = self.checked(row, plan)
            previous = value.get("wireguard_reservation")
            if previous:
                if previous["kube_system_uid"] != metadata["kube_system_uid"]:
                    raise ProviderIdentityConflict(
                        "Reserved address belongs to another cluster identity"
                    )
                return previous["address"]
            if value["state"] != "allocated" or value.get("termination_authorization"):
                raise ProviderIdentityConflict("Address reservation requires an active allocation")
            for other in db.scalars(select(ProviderIntent)):
                allocation = other.payload.get("lambda_launch", {})
                if allocation.get("state") != "terminated" and allocation.get(
                    "wireguard_reservation"
                ):
                    occupied.add(allocation["wireguard_reservation"]["address"])
            address = next(
                (f"10.200.0.{i}" for i in range(67, 255) if f"10.200.0.{i}" not in occupied), None
            )
            if address is None:
                raise CapacityUnavailable("No unreserved GPU WireGuard addresses remain")
            row.payload = {
                **row.payload,
                "lambda_launch": {
                    **value,
                    "wireguard_reservation": {
                        "address": address,
                        "kube_system_uid": metadata["kube_system_uid"],
                        "registry_revision": metadata["registry_revision"],
                        "reserved_at": now().isoformat(),
                    },
                },
            }
            return address

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
        if (
            len(identities) != 1
            or not isinstance(identities[0], str)
            or not re.fullmatch(r"[a-f0-9]{32}", identities[0])
        ):
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

    def start_termination(self, plan):
        """Commit intent before provider I/O; a fenced VM still reserves its mesh address."""
        with session() as db, db.begin():
            row = db.get(ProviderIntent, plan.intent_id, with_for_update=True)
            value = self.checked(row, plan)
            if value["state"] in {"terminating", "fenced", "terminated"}:
                return copy.deepcopy(value)
            if value["state"] != "allocated" or len(value["instance_ids"]) != 1:
                raise ProviderIdentityConflict(
                    "Termination requires an identified owned allocation"
                )
            if not value.get("termination_authorization"):
                raise ProviderIdentityConflict("Termination requires trusted host safety evidence")
            value = {**value, "state": "terminating", "termination_requested_at": now().isoformat()}
            row.state, row.payload = "terminating", {**row.payload, "lambda_launch": value}
            return copy.deepcopy(value)

    @staticmethod
    def check_host_receipt(value, task: HostTask, observation: HostTaskObservation):
        document = task.model_dump(mode="json")
        if document.get("provider_fence") is None:
            document.pop("provider_fence", None)
        if (
            value["instance_ids"] != [task.provider_instance_id]
            or observation.task_id != task.id
            or observation.input_sha256 != digest(document)
            or observation.status != "completed"
            or observation.cleanup_pending
            or observation.evidence_sha256 is None
            or observation.gpu_runtime_ready
            or (task.node_uid and observation.node_uid != task.node_uid)
        ):
            raise ProviderIdentityConflict("Host safety receipt differs from the owned task")
        saved = value.get("host_tasks", {}).get(task.id)
        if (
            saved is None
            or saved.get("task") != task.model_dump(mode="json")
            or saved.get("observation") != observation.model_dump(mode="json")
            or value.get("active_host_task_id") != task.id
        ):
            raise ProviderIdentityConflict(
                "Host safety receipt is not the durable active execution"
            )

    def authorize_termination(self, plan, task: HostTask, observation: HostTaskObservation):
        with session() as db, db.begin():
            row = db.get(ProviderIntent, plan.intent_id, with_for_update=True)
            value = self.checked(row, plan)
            authorization = {
                "task_id": task.id,
                "input_sha256": observation.input_sha256,
                "observation_sha256": digest(observation.model_dump(mode="json")),
                "route": "bootstrap" if task.action == "cleanup-check" else "drained",
            }
            if value.get("termination_authorization"):
                if value["termination_authorization"] != authorization:
                    raise ProviderIdentityConflict("Termination authorization is immutable")
                return
            self.check_host_receipt(value, task, observation)
            if not (
                (task.action == "cleanup-check" and observation.bootstrap_fence_permitted)
                or (task.action == "remove" and task.node_uid is not None)
            ):
                raise ProviderIdentityConflict(
                    "Termination needs bootstrap safety or drained removal"
                )
            if value["state"] != "allocated":
                raise ProviderIdentityConflict(
                    "Cannot replace authorization after termination starts"
                )
            row.payload = {
                **row.payload,
                "lambda_launch": {**value, "termination_authorization": authorization},
            }

    def authorize_unbootstrapped(self, plan):
        """No dispatched host task means this VM never received cluster join material.

        Absence of the field on historical documents is not proof. Every real
        host task must be frozen in this ledger before its first external call.
        """
        with session() as db, db.begin():
            row = db.get(ProviderIntent, plan.intent_id, with_for_update=True)
            value = self.checked(row, plan)
            authorization = {"route": "unbootstrapped"}
            if value.get("termination_authorization") == authorization:
                return
            if (
                value["state"] != "allocated"
                or value.get("host_tasks") != {}
                or value.get("active_host_task_id") is not None
                or value.get("termination_authorization")
            ):
                raise ProviderIdentityConflict("Cannot prove absence of host bootstrap dispatch")
            row.payload = {
                **row.payload,
                "lambda_launch": {**value, "termination_authorization": authorization},
            }

    def unbootstrapped_cleanup_completed(self, plan):
        with session() as db, db.begin():
            row = db.get(ProviderIntent, plan.intent_id, with_for_update=True)
            value = self.checked(row, plan)
            if (
                value["state"] not in {"fenced", "terminated"}
                or value.get("termination_authorization") != {"route": "unbootstrapped"}
                or value.get("host_tasks") != {}
                or value.get("active_host_task_id") is not None
            ):
                raise ProviderIdentityConflict("Unbootstrapped cleanup lacks provider fencing")
            if value["state"] == "terminated":
                return
            row.state = "terminated"
            row.payload = {
                **row.payload,
                "lambda_launch": {
                    **value,
                    "state": "terminated",
                    "cleanup_completed_at": now().isoformat(),
                    "cleanup_kind": "no-host-dispatch",
                },
            }

    def termination_acknowledged(self, plan, observed):
        with session() as db, db.begin():
            row = db.get(ProviderIntent, plan.intent_id, with_for_update=True)
            value = self.checked(row, plan)
            if value["state"] != "terminating" or observed.get("id") not in value["instance_ids"]:
                raise ProviderIdentityConflict(
                    "Termination acknowledgment changed the owned instance"
                )
            row.payload = {
                **row.payload,
                "lambda_launch": {
                    **value,
                    "termination_acknowledgment": {
                        "at": now().isoformat(),
                        "sha256": digest(observed),
                    },
                },
            }

    def fenced(self, plan, evidence):
        with session() as db, db.begin():
            row = db.get(ProviderIntent, plan.intent_id, with_for_update=True)
            value = self.checked(row, plan)
            if value.get("provider_fence"):
                return ProviderFence.model_validate(value["provider_fence"])
            if value["state"] != "terminating":
                raise ProviderIdentityConflict(
                    "Provider fencing requires committed termination intent"
                )
            receipt = ProviderFence(
                intent_id=plan.intent_id,
                provider_instance_id=value["instance_ids"][0],
                termination_requested_at=value["termination_requested_at"],
                confirmed_at=now(),
                observation_sha256=digest(evidence),
            )
            row.state = "fenced"
            row.payload = {
                **row.payload,
                "lambda_launch": {
                    **value,
                    "state": "fenced",
                    "provider_fence": receipt.model_dump(mode="json"),
                },
            }
            return receipt

    def cleanup_completed(self, plan, task: HostTask, observation: HostTaskObservation):
        """Release capacity only after the executor confirms network/identity removal."""
        with session() as db, db.begin():
            row = db.get(ProviderIntent, plan.intent_id, with_for_update=True)
            value = self.checked(row, plan)
            self.check_host_receipt(value, task, observation)
            if value["state"] == "terminated":
                if value.get("cleanup_observation_sha256") != digest(
                    observation.model_dump(mode="json")
                ):
                    raise ProviderIdentityConflict("Terminal cleanup evidence changed")
                return
            authorization = value.get("termination_authorization", {})
            valid_bootstrap = (
                (
                    authorization.get("route") == "bootstrap"
                    and task.action == "revoke-bootstrap"
                    and observation.bootstrap_cleanup_complete
                    and task.provider_fence == ProviderFence.model_validate(value["provider_fence"])
                )
                if value.get("provider_fence")
                else False
            )
            valid_drained = (
                authorization.get("route") == "drained"
                and task.action == "remove"
                and authorization.get("task_id") == task.id
                and authorization.get("observation_sha256")
                == digest(observation.model_dump(mode="json"))
            )
            if value["state"] != "fenced" or not (valid_bootstrap or valid_drained):
                raise ProviderIdentityConflict(
                    "Mesh cleanup is not complete; reservation remains held"
                )
            row.state = "terminated"
            row.payload = {
                **row.payload,
                "lambda_launch": {
                    **value,
                    "state": "terminated",
                    "cleanup_completed_at": now().isoformat(),
                    "cleanup_observation_sha256": digest(observation.model_dump(mode="json")),
                },
            }


def allocate_once(plan, user_data, ledger, api):
    """Crash-safe launch coordinator. Repeated calls reconcile the original intent."""
    ledger.freeze(plan)
    current = ledger.read(plan)
    if current["state"] in {"allocated", "terminating", "fenced", "terminated"}:
        return tuple(
            current["instance_ids"]
        )  # Historical attempt; no credentials/readiness needed.
    if current["state"] == "rejected":
        raise ProviderRejected("The original allocation was rejected; create a new operation")
    if current["state"] == "pending":
        plan.request(user_data)  # Check secret-file digest before reserving capacity.
    instances = api.instances()
    observed = reconcile_launch(current["state"], plan, instances, current["instance_ids"])
    if observed:
        ledger.allocated(plan, [i["id"] for i in observed])
        return tuple(i["id"] for i in observed)
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


def terminate_once(plan, ledger, api):
    """Advance one owned termination; repeated calls never allocate or restart hosts.

    A successful POST only acknowledges the request. Fencing requires a fresh
    complete list plus an exact-instance terminal observation or documented 404.
    The caller must complete the bootstrap safety check / normal drain first.
    """
    current = ledger.start_termination(plan)
    if current.get("provider_fence"):
        return ProviderFence.model_validate(current["provider_fence"])
    identities = current["instance_ids"]
    instances = api.instances()
    owned = owned_instances(plan, instances, identities)
    specific = api.instance(identities[0])
    if specific is not None:
        owned_instances(plan, [specific], identities)
    terminal = {"terminated", "preempted"}
    if (not owned or owned[0].get("status") in terminal) and (
        specific is None or specific.get("status") in terminal
    ):
        return ledger.fenced(
            plan, {"list": owned, "instance": specific, "observed_at": now().isoformat()}
        )
    # If the reads disagree, wait for reconciliation; do not terminate a new identity.
    if not owned or specific is None:
        return None
    if not current.get("termination_acknowledgment"):
        response = api.terminate(identities[0])
        ledger.termination_acknowledged(plan, response[0])
    return None
