"""Free provider prerequisites with exact ownership and no paid mutation path."""

import hashlib
import json
import re
from ipaddress import IPv4Network
from typing import Literal

from platform_contracts.common import ContractModel, Sha256
from platform_contracts.host_operations import ed25519_host_key
from pydantic import Field, model_validator

from platform_management.providers.lambda_mutations import (
    LambdaMutations,
    ProviderIdentityConflict,
    ProviderRateLimited,
    ProviderRejected,
)
from platform_management.services.common import digest

PROVIDER_ID = r"(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})"
CPU_NAMES = {"cp-01", "cp-02", "cp-03", "platform-01", "platform-02", "platform-03"}


class PrerequisiteSpec(ContractModel):
    schema_version: Literal["klip-lambda-prerequisites.v1"] = "klip-lambda-prerequisites.v1"
    cluster_id: Literal["klip"] = "klip"
    kube_system_uid: str = Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")
    region: Literal["us-east-1"] = "us-east-1"
    provider_credential_sha256: Sha256
    public_key: str = Field(min_length=50, max_length=512)
    cpu_public_ips: dict[str, IPv4Network]

    @model_validator(mode="after")
    def checked_scope(self):
        if (
            set(self.cpu_public_ips) != CPU_NAMES
            or any(
                network.prefixlen != 32 or not network.network_address.is_global
                for network in self.cpu_public_ips.values()
            )
            or len(set(self.cpu_public_ips.values())) != 6
        ):
            raise ValueError("Provider prerequisites require six distinct fixed public CPU /32s")
        if ed25519_host_key(self.public_key) != self.public_key:
            raise ValueError("Only the executor's canonical public Ed25519 key may be registered")
        return self

    @property
    def identity(self):
        return "lambda-setup-" + digest(self.model_dump(mode="json"))[:24]

    @property
    def names(self):
        identity = self.kube_system_uid.replace("-", "")[:12]
        key = hashlib.sha256(self.public_key.encode()).hexdigest()[:12]
        network = digest({k: str(v) for k, v in self.cpu_public_ips.items()})[:12]
        return {
            "ssh": "klip-a10-ssh-" + identity + "-" + key,
            "firewall": "klip-a10-bootstrap-" + identity + "-" + network,
        }

    def body(self, resource):
        if resource == "ssh":
            return {"name": self.names[resource], "public_key": self.public_key}
        if resource != "firewall":
            raise ValueError("Unknown prerequisite resource")
        rules = []
        for name, network in sorted(self.cpu_public_ips.items()):
            rules.append(
                {
                    "protocol": "udp",
                    "port_range": [51820, 51820],
                    "source_network": str(network),
                    "description": "KLIP WireGuard " + name,
                }
            )
            if name.startswith("platform-"):
                rules.append(
                    {
                        "protocol": "tcp",
                        "port_range": [22, 22],
                        "source_network": str(network),
                        "description": "KLIP host executor " + name,
                    }
                )
        return {"name": self.names[resource], "region": self.region, "rules": rules}


def normalized_rules(rules):
    if not isinstance(rules, list) or len(rules) > 100:
        raise ProviderIdentityConflict("Provider firewall rules are malformed or unbounded")
    result = []
    for item in rules:
        if not isinstance(item, dict) or item.get("protocol") not in {"tcp", "udp", "icmp", "all"}:
            raise ProviderIdentityConflict("Provider firewall protocol is malformed")
        try:
            network = IPv4Network(item["source_network"], strict=True)
        except (ValueError, KeyError, TypeError):
            raise ProviderIdentityConflict("Provider firewall source is malformed") from None
        ports = item.get("port_range")
        if item["protocol"] == "icmp":
            if ports is not None:
                raise ProviderIdentityConflict("ICMP firewall rules cannot specify ports")
        elif (
            not isinstance(ports, list)
            or len(ports) != 2
            or any(type(p) is not int or not 1 <= p <= 65535 for p in ports)
            or ports[0] > ports[1]
        ):
            raise ProviderIdentityConflict("Provider firewall ports are malformed")
        description = item.get("description")
        if not isinstance(description, str) or len(description) > 128:
            raise ProviderIdentityConflict("Provider firewall description is malformed")
        result.append(
            {
                "protocol": item["protocol"],
                "port_range": ports,
                "source_network": str(network),
                "description": description,
            }
        )
    encoded = [json.dumps(v, sort_keys=True) for v in result]
    if len(set(encoded)) != len(encoded):
        raise ProviderIdentityConflict("Provider firewall has duplicate rules")
    return sorted(result, key=lambda item: json.dumps(item, sort_keys=True))


class LambdaPrerequisites(LambdaMutations):
    """The inherited transport enforces bounds, pacing, fixed origin and no retries."""

    def allowed_request(self, method, path):
        return (
            method == "GET"
            and path in {"account", "ssh-keys", "firewall-rulesets", "firewall-rulesets/global"}
        ) or (method == "POST" and path in {"ssh-keys", "firewall-rulesets"})

    def account(self):
        body, _ = self.request("GET", "account")
        identity = body.get("id") if isinstance(body, dict) else None
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
            raise ProviderIdentityConflict("Provider account identity is missing or malformed")
        return identity

    def global_firewall(self):
        body, _ = self.request("GET", "firewall-rulesets/global")
        if not isinstance(body, dict) or body.get("id") != "global":
            raise ProviderIdentityConflict("Provider global firewall identity is malformed")
        rules = normalized_rules(body.get("rules"))
        return {
            "id": "global",
            "rules_sha256": digest(rules),
            "rules": [{k: v for k, v in rule.items() if k != "description"} for rule in rules],
        }

    def resources(self, resource):
        path = {"ssh": "ssh-keys", "firewall": "firewall-rulesets"}[resource]
        body, following = self.request("GET", path)
        if (
            following
            or not isinstance(body, list)
            or len(body) > 256
            or any(not isinstance(item, dict) for item in body)
        ):
            raise ProviderIdentityConflict(
                "Provider prerequisite inventory is malformed or incomplete"
            )
        identities = [i.get("id") for i in body]
        if any(
            not isinstance(i, str) or not re.fullmatch(PROVIDER_ID, i) for i in identities
        ) or len(set(identities)) != len(identities):
            raise ProviderIdentityConflict(
                "Provider prerequisite inventory duplicates or omits identity"
            )
        # In particular, never retain private_key or arbitrary API response fields.
        fields = (
            {"id", "name", "public_key"} if resource == "ssh" else {"id", "name", "region", "rules"}
        )
        return [{k: v for k, v in item.items() if k in fields} for item in body]

    def create(self, spec, resource):
        path = {"ssh": "ssh-keys", "firewall": "firewall-rulesets"}[resource]
        # Supplying public_key prevents Lambda from generating/returning a private key.
        self.request("POST", path, body=spec.body(resource))


def matching(spec, resource, values, known_id=None):
    matches = [item for item in values if item.get("name") == spec.names[resource]]
    if len(matches) > 1:
        raise ProviderIdentityConflict("Provider prerequisite name is ambiguous")
    if not matches:
        if known_id is not None:
            raise ProviderIdentityConflict("Previously recorded provider prerequisite disappeared")
        return None
    item, expected = matches[0], spec.body(resource)
    if known_id is not None and item.get("id") != known_id:
        raise ProviderIdentityConflict("Recorded provider prerequisite identity changed")
    if resource == "ssh":
        if item.get("public_key") != expected["public_key"]:
            raise ProviderIdentityConflict("Provider SSH public key differs from its frozen intent")
    else:
        region = item.get("region")
        if (
            not isinstance(region, dict)
            or region.get("name") != spec.region
            or normalized_rules(item.get("rules")) != normalized_rules(expected["rules"])
        ):
            raise ProviderIdentityConflict("Provider firewall scope differs from its frozen intent")
    return item["id"]


def prepare_once(spec, api, ledger):
    """Caller holds the setup ownership lock; every HTTP call holds request_gate.

    An uncertain POST is reconciled by exact inventory, never sent again merely
    because a later GET is empty. Existing, unowned names are never adopted.
    """
    ledger.freeze(spec)
    account = api.account()
    ledger.bind_account(spec, account)
    global_firewall = api.global_firewall()
    ledger.observe_global(spec, global_firewall)
    for resource in ("ssh", "firewall"):
        value = ledger.read(spec)["resources"][resource]
        state = value["state"]
        if state == "rejected":
            raise ProviderRejected(
                "Prerequisite intent was definitively rejected; explicit recovery required"
            )
        identifier = matching(spec, resource, api.resources(resource), value.get("id"))
        if state == "pending":
            if identifier is not None:
                raise ProviderIdentityConflict(
                    "Refusing to adopt an existing, unowned prerequisite name"
                )
            ledger.record(spec, resource, "dispatching")  # Commit before POST.
            try:
                api.create(spec, resource)
            except (ProviderRejected, ProviderRateLimited) as error:
                ledger.record(spec, resource, "rejected", error_type=type(error).__name__)
                raise
            identifier = matching(spec, resource, api.resources(resource))
        if identifier is None:
            raise ProviderIdentityConflict(
                "Provider prerequisite dispatch remains unresolved; no POST retry is allowed"
            )
        ledger.record(spec, resource, "ready", id=identifier)
    return ledger.complete(spec)
