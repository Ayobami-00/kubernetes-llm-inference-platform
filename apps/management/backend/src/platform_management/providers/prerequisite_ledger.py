"""Provider setup intent and ownership live in the existing management database."""

import copy
from contextlib import contextmanager

from sqlalchemy import select, text

from platform_management.db.models import Preference
from platform_management.db.session import engine, session
from platform_management.providers.lambda_mutations import ProviderIdentityConflict
from platform_management.services.common import digest, lock_key, now

PREFIX = "lambda-prerequisites:"


@contextmanager
def setup_owner():
    # Separate from the request-spacing lock: no transaction is held during HTTP.
    key = lock_key("lambda-provider-prerequisites-v1")
    with engine().connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


class PostgresPrerequisiteLedger:
    def checked(self, row, spec):
        if row is None or row.payload.get("spec_sha256") != digest(spec.model_dump(mode="json")):
            raise ProviderIdentityConflict("Provider setup intent is missing or changed")
        return copy.deepcopy(row.payload)

    def freeze(self, spec):
        with session() as db, db.begin():
            row = db.get(Preference, PREFIX + spec.identity, with_for_update=True)
            if row:
                self.checked(row, spec)
                return
            for previous in db.scalars(select(Preference).where(Preference.id.startswith(PREFIX))):
                if previous.payload.get("state") not in {"ready", "rejected"}:
                    raise ProviderIdentityConflict(
                        "Reconcile the original provider setup before creating another"
                    )
            db.add(
                Preference(
                    id=PREFIX + spec.identity,
                    payload={
                        "schema_version": "klip-lambda-prerequisite-result.v1",
                        "setup_id": spec.identity,
                        "spec": spec.model_dump(mode="json"),
                        "spec_sha256": digest(spec.model_dump(mode="json")),
                        "created_at": now().isoformat(),
                        "state": "preparing",
                        "account_id": None,
                        "resources": {name: {"state": "pending"} for name in ("ssh", "firewall")},
                    },
                )
            )

    def read(self, spec):
        with session() as db:
            return self.checked(db.get(Preference, PREFIX + spec.identity), spec)

    def update(self, spec, transform):
        with session() as db, db.begin():
            row = db.get(Preference, PREFIX + spec.identity, with_for_update=True)
            value = self.checked(row, spec)
            transform(value)
            row.payload = value

    def bind_account(self, spec, identity):
        def bind(value):
            if value["account_id"] not in {None, identity}:
                raise ProviderIdentityConflict("Bound provider account identity changed")
            value["account_id"] = identity

        self.update(spec, bind)

    def observe_global(self, spec, observed):
        def observe(value):
            value["global_firewall"] = {
                **observed,
                "observed_at": now().isoformat(),
                "ownership": "external",
                "mutations_performed": False,
                "limitation": (
                    "Global rules can broaden provider ingress. Dedicated rules do not "
                    "establish host or gateway readiness."
                ),
            }

        self.update(spec, observe)

    def record(self, spec, resource, state, **fields):
        if resource not in {"ssh", "firewall"} or state not in {"dispatching", "ready", "rejected"}:
            raise ValueError("Invalid provider setup transition")

        def record(value):
            previous = value["resources"][resource]
            allowed = {
                "pending": {"dispatching"},
                "dispatching": {"ready", "rejected"},
                "ready": {"ready"},
            }
            if state not in allowed.get(previous["state"], set()):
                raise ProviderIdentityConflict("Invalid or repeated provider setup dispatch")
            if previous.get("id") is not None and previous["id"] != fields.get("id"):
                raise ProviderIdentityConflict("Previously recorded provider resource changed")
            value["resources"][resource] = {
                **previous,
                **fields,
                "state": state,
                "observed_at": now().isoformat(),
            }
            if state == "rejected":
                value["state"] = "rejected"

        self.update(spec, record)

    def complete(self, spec):
        def finish(value):
            if any(v["state"] != "ready" for v in value["resources"].values()):
                raise ProviderIdentityConflict("Provider prerequisites are incomplete")
            value["state"] = "ready"
            value.setdefault("completed_at", now().isoformat())
            value["ownership"] = "management"
            value["gpu_capacity_provisioned"] = False

        self.update(spec, finish)
        return self.read(spec)
