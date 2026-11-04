"""Real PostgreSQL transactions coordinate free setup against synthetic provider I/O."""

import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from platform_management.db.models import Preference
from platform_management.db.session import session
from platform_management.providers.base import TransientProviderError
from platform_management.providers.lambda_mutations import ProviderIdentityConflict
from platform_management.providers.lambda_prerequisites import (
    CPU_NAMES,
    PrerequisiteSpec,
    prepare_once,
)
from platform_management.providers.prerequisite_ledger import (
    PREFIX,
    PostgresPrerequisiteLedger,
    setup_owner,
)


def spec():
    key = Ed25519PrivateKey.from_private_bytes(b"test-only-fixed-public-key-source"[:32])
    return PrerequisiteSpec(
        kube_system_uid="11111111-2222-3333-4444-555555555555",
        provider_credential_sha256="a" * 64,
        public_key=key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode(),
        cpu_public_ips={n: f"8.8.8.{i}/32" for i, n in enumerate(sorted(CPU_NAMES), 1)},
    )


class API:
    def __init__(self, value):
        self.spec = value
        self.values = {"ssh": [], "firewall": []}
        self.posts = []
        self.drop = False
        self.account_id = "synthetic-provider-account"

    def account(self):
        return self.account_id

    def global_firewall(self):
        return {"id": "global", "rules": [], "rules_sha256": "d" * 64}

    def resources(self, kind):
        return copy.deepcopy(self.values[kind])

    def create(self, spec, kind):
        # This independent transaction proves intent is committed before external I/O.
        with session() as db:
            row = db.get(Preference, PREFIX + spec.identity)
            assert row.payload["resources"][kind]["state"] == "dispatching"
            assert row.payload["account_id"] == self.account_id
        self.posts.append(kind)
        result = {**spec.body(kind), "id": ("b" if kind == "ssh" else "c") * 32}
        if kind == "firewall":
            result["region"] = {"name": spec.region}
        self.values[kind].append(result)
        if self.drop:
            self.drop = False
            raise TransientProviderError("Synthetic lost provider response")


def test_committed_intent_and_concurrent_setup_produce_one_create_per_resource():
    value = spec()
    api = API(value)

    def execute():
        with setup_owner():
            return prepare_once(value, api, PostgresPrerequisiteLedger())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: execute(), range(2)))
    assert all(r["state"] == "ready" for r in results)
    assert api.posts == ["ssh", "firewall"]
    assert all(r["gpu_capacity_provisioned"] is False for r in results)


def test_lost_post_response_reuses_committed_dispatch_and_never_reposts():
    value = spec()
    api = API(value)
    api.drop = True
    with setup_owner(), pytest.raises(TransientProviderError):
        prepare_once(value, api, PostgresPrerequisiteLedger())
    with setup_owner():
        result = prepare_once(value, api, PostgresPrerequisiteLedger())
    assert result["state"] == "ready" and api.posts == ["ssh", "firewall"]


def test_changed_account_or_new_plan_cannot_bypass_unresolved_ownership():
    value = spec()
    api = API(value)
    api.drop = True
    with setup_owner(), pytest.raises(TransientProviderError):
        prepare_once(value, api, PostgresPrerequisiteLedger())
    changed = value.model_copy(update={"provider_credential_sha256": "e" * 64})
    with setup_owner(), pytest.raises(ProviderIdentityConflict):
        prepare_once(changed, api, PostgresPrerequisiteLedger())
    api.account_id = "another-account"
    with setup_owner(), pytest.raises(ProviderIdentityConflict):
        prepare_once(value, api, PostgresPrerequisiteLedger())
    assert api.posts == ["ssh"]
