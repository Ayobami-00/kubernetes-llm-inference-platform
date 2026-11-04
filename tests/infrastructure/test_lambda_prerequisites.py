"""Free provider setup cannot rent capacity, retry an uncertain POST or adopt foreign names."""

import copy
import json
from contextlib import contextmanager

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from platform_management.integrations.lambda_policy import LambdaLifecyclePolicy
from platform_management.providers.base import TransientProviderError
from platform_management.providers.lambda_mutations import ProviderIdentityConflict
from platform_management.providers.lambda_prerequisites import (
    CPU_NAMES,
    LambdaPrerequisites,
    PrerequisiteSpec,
    matching,
    prepare_once,
)


@pytest.fixture
def spec():
    key = Ed25519PrivateKey.from_private_bytes(b"test-only-fixed-public-key-source"[:32])
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    return PrerequisiteSpec(
        kube_system_uid="11111111-2222-3333-4444-555555555555",
        provider_credential_sha256="a" * 64,
        public_key=public,
        cpu_public_ips={name: f"8.8.8.{i}/32" for i, name in enumerate(sorted(CPU_NAMES), 1)},
    )


class Ledger:
    def __init__(self):
        self.value = None

    def freeze(self, spec):
        if self.value is None:
            self.value = {
                "state": "preparing",
                "account_id": None,
                "resources": {r: {"state": "pending"} for r in ("ssh", "firewall")},
            }

    def read(self, spec):
        return copy.deepcopy(self.value)

    def bind_account(self, spec, account):
        if self.value["account_id"] not in {None, account}:
            raise ProviderIdentityConflict("Changed account")
        self.value["account_id"] = account

    def observe_global(self, spec, value):
        self.value["global_firewall"] = value

    def record(self, spec, resource, state, **values):
        self.value["resources"][resource] = {"state": state, **values}

    def complete(self, spec):
        assert all(v["state"] == "ready" for v in self.value["resources"].values())
        self.value["state"] = "ready"
        return self.read(spec)


@pytest.fixture
def setup(spec):
    ledger, values, requests, gates = Ledger(), {"ssh": [], "firewall": []}, [], []
    fault = {"drop": None, "missing": False, "account": "fixture-account"}

    @contextmanager
    def gate(*, launch):
        assert launch is False
        gates.append(True)
        yield

    def handler(request):
        requests.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer synthetic-key"
        path = request.url.path.removeprefix("/api/v1/")
        if path == "account":
            data = {"id": fault["account"], "name": "must-not-retain"}
        elif path == "firewall-rulesets/global":
            data = {
                "id": "global",
                "rules": [
                    {
                        "protocol": "tcp",
                        "port_range": [22, 22],
                        "source_network": "0.0.0.0/0",
                        "description": "sensitive-untrusted-comment",
                    }
                ],
            }
        else:
            resource = {"ssh-keys": "ssh", "firewall-rulesets": "firewall"}[path]
            if request.method == "POST":
                assert ledger.read(spec)["resources"][resource]["state"] == "dispatching"
                body = json.loads(request.content)
                assert body == spec.body(resource)
                result = {**body, "id": ("b" if resource == "ssh" else "c") * 32}
                if resource == "firewall":
                    result["region"] = {"name": spec.region}
                values[resource].append(result)
                if fault["drop"] == resource:
                    fault["drop"] = None
                    raise httpx.ReadTimeout("must-not-log-request", request=request)
                data = {"id": result["id"], "private_key": "must-not-retain"}
            else:
                data = [] if fault["missing"] else values[resource]
        return httpx.Response(200, json={"data": data})

    api = LambdaPrerequisites("synthetic-key", gate=gate, transport=httpx.MockTransport(handler))
    yield api, ledger, values, requests, gates, fault
    api.close()


def test_real_transport_prepares_exact_public_prerequisites_once(spec, setup):
    api, ledger, values, requests, gates, fault = setup
    result = prepare_once(spec, api, ledger)
    posts = [r for r in requests if r[0] == "POST"]
    assert len(posts) == 2 and result["state"] == "ready"
    prepare_once(spec, api, ledger)
    assert [r for r in requests if r[0] == "POST"] == posts
    assert len(gates) == len(requests)
    assert "sensitive-untrusted-comment" not in json.dumps(result)
    assert "private_key" not in json.dumps(result)
    assert len(spec.body("firewall")["rules"]) == 9


@pytest.mark.parametrize("resource", ["ssh", "firewall"])
def test_lost_create_response_is_reconciled_without_reposting(spec, setup, resource):
    api, ledger, values, requests, gates, fault = setup
    fault["drop"] = resource
    with pytest.raises(TransientProviderError):
        prepare_once(spec, api, ledger)
    count = len([r for r in requests if r[0] == "POST"])
    assert ledger.value["resources"][resource]["state"] == "dispatching"
    assert prepare_once(spec, api, ledger)["state"] == "ready"
    assert len([r for r in requests if r[0] == "POST"]) == 2
    assert count == (1 if resource == "ssh" else 2)


def test_empty_inventory_never_authorizes_another_uncertain_create(spec, setup):
    api, ledger, values, requests, gates, fault = setup
    fault["drop"] = "ssh"
    with pytest.raises(TransientProviderError):
        prepare_once(spec, api, ledger)
    fault["missing"] = True
    with pytest.raises(ProviderIdentityConflict):
        prepare_once(spec, api, ledger)
    assert len([r for r in requests if r[0] == "POST"]) == 1


def test_pending_intent_cannot_adopt_a_preexisting_name(spec, setup):
    api, ledger, values, requests, gates, fault = setup
    values["ssh"].append({**spec.body("ssh"), "id": "b" * 32})
    with pytest.raises(ProviderIdentityConflict, match="unowned"):
        prepare_once(spec, api, ledger)
    assert not any(r[0] == "POST" for r in requests)


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "instance-operations/launch"),
        ("POST", "instance-operations/terminate"),
        ("PATCH", "firewall-rulesets/global"),
        ("DELETE", "ssh-keys/" + "b" * 32),
        ("GET", "https://other.invalid/"),
    ],
)
def test_setup_transport_has_no_paid_destructive_or_cross_origin_path(setup, method, path):
    api, ledger, values, requests, gates, fault = setup
    with pytest.raises(ValueError):
        api.request(method, path)
    assert not requests and not gates


@pytest.mark.parametrize("resource", ["ssh", "firewall"])
def test_changed_or_duplicated_resources_are_not_ready(spec, resource):
    item = {**spec.body(resource), "id": "b" * 32}
    if resource == "firewall":
        item["region"] = {"name": spec.region}
    assert matching(spec, resource, [item], "b" * 32) == "b" * 32
    with pytest.raises(ProviderIdentityConflict):
        matching(spec, resource, [item, item])
    with pytest.raises(ProviderIdentityConflict):
        matching(spec, resource, [item], "c" * 32)
    if resource == "ssh":
        item["public_key"] += " changed-comment"
    else:
        item["rules"][0]["source_network"] = "0.0.0.0/0"
    with pytest.raises(ProviderIdentityConflict):
        matching(spec, resource, [item])


def test_firewall_provider_ids_accept_documented_hex_and_uuid_forms():
    fields = {
        "kube_system_uid": "11111111-2222-3333-4444-555555555555",
        "image_id": "11111111-2222-3333-4444-555555555555",
        "ssh_key_name": "klip-test",
        "ssh_public_key_sha256": "a" * 64,
    }
    for identity in ("b" * 32, "11111111-2222-3333-4444-555555555555"):
        assert LambdaLifecyclePolicy(**fields, firewall_ruleset_ids=(identity,))
    with pytest.raises(ValueError):
        LambdaLifecyclePolicy(**fields, firewall_ruleset_ids=("../global",))


def test_cli_accepts_kubernetes_projected_inputs_but_rejects_directory_escape(spec, tmp_path):
    from platform_management.jobs.provider_prerequisites import load_spec

    root = tmp_path / "config"
    root.mkdir()
    revision = root / "..2026-test"
    revision.mkdir()
    (revision / "provider-setup.json").write_text(spec.model_dump_json())
    (root / "..data").symlink_to(revision.name, target_is_directory=True)
    path = root / "provider-setup.json"
    path.symlink_to("..data/provider-setup.json")
    assert load_spec(path) == spec
    external = tmp_path / "external.json"
    external.write_text(spec.model_dump_json())
    path.unlink()
    path.symlink_to(external)
    with pytest.raises(ValueError, match="Unsafe"):
        load_spec(path)
