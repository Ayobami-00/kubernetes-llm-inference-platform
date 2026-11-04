"""Bounded Lambda transport and fail-closed allocation reconciliation.

The caller must commit ``dispatching`` before POST, under its durable ownership
lock. Lambda has no launch idempotency key: absence after a lost response is not
permission to submit another launch. This module never retries HTTP requests.
"""

import re
from contextlib import nullcontext
from typing import Literal, Self

import httpx
from platform_contracts.common import ContractModel, NonEmpty
from pydantic import Field, model_validator

from platform_management.providers.base import (
    AllocationAmbiguous,
    CapacityUnavailable,
    TransientProviderError,
)

BASE_URL = "https://cloud.lambda.ai/api/v1/"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def safe_instance(instance: dict) -> dict:
    """Do not retain Jupyter tokens, URLs, user data or unknown provider fields."""
    safe = {
        k: instance[k]
        for k in ("id", "name", "ip", "private_ip", "status", "hostname", "ssh_key_names")
        if k in instance
    }
    for name, fields in (
        ("region", ("name",)),
        ("instance_type", ("name",)),
        ("image", ("id", "family")),
    ):
        if name in instance:
            value = instance[name]
            if not isinstance(value, dict):
                raise ValueError("Malformed provider instance metadata")
            safe[name] = {k: value[k] for k in fields if k in value}
    for name, fields in (("tags", ("key", "value")), ("firewall_rulesets", ("id",))):
        if name in instance:
            value = instance[name]
            if not isinstance(value, list) or any(not isinstance(i, dict) for i in value):
                raise ValueError("Malformed provider instance metadata")
            safe[name] = [{k: i[k] for k in fields if k in i} for i in value]
    return safe


class ProviderRejected(RuntimeError):
    """A definite API rejection. Messages never include response bodies."""


class ProviderNotFound(ProviderRejected):
    """A specific instance read returned the documented object-does-not-exist code."""


class ProviderRateLimited(TransientProviderError):
    """The API definitively rejected this request before allocation."""


class ProviderIdentityConflict(RuntimeError):
    """A name, tag or immutable provider identity disagrees with durable intent."""


class LaunchPlan(ContractModel):
    schema_version: Literal["klip-lambda-launch.v1"] = "klip-lambda-launch.v1"
    intent_id: str = Field(pattern=r"^op-[a-f0-9]{16,32}$")
    cluster_id: Literal["klip"] = "klip"
    instance_type: NonEmpty
    region: NonEmpty
    image_id: NonEmpty
    ssh_key_name: NonEmpty
    firewall_ruleset_ids: tuple[NonEmpty, ...] = Field(min_length=1, max_length=4)
    # One independently owned allocation per intent, including the first A10.
    quantity: Literal[1] = 1
    lease_minutes: int = Field(default=90, strict=True, ge=1, le=90)
    user_data_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def unique_rules(self) -> Self:
        if len(set(self.firewall_ruleset_ids)) != len(self.firewall_ruleset_ids):
            raise ValueError("Duplicate provider firewall identity")
        return self

    @property
    def name(self):
        return self.cluster_id + "-" + self.intent_id

    @property
    def tags(self):
        return {"klip-cluster": self.cluster_id, "klip-operation": self.intent_id}

    def request(self, user_data: str) -> dict:
        import hashlib

        if (
            not user_data.startswith("#cloud-config\n")
            or len(user_data.encode()) > 1024 * 1024
            or hashlib.sha256(user_data.encode()).hexdigest() != self.user_data_sha256
        ):
            raise ValueError("Launch cloud-init differs from its frozen digest or size limit")
        return {
            "region_name": self.region,
            "instance_type_name": self.instance_type,
            "ssh_key_names": [self.ssh_key_name],
            "file_system_names": [],
            "quantity": self.quantity,
            "name": self.name,
            "hostname": self.name,
            "image": {"id": self.image_id},
            "firewall_rulesets": [{"id": i} for i in self.firewall_ruleset_ids],
            "tags": [{"key": k, "value": v} for k, v in self.tags.items()],
            "user_data": user_data,
        }


class LambdaMutations:
    def __init__(self, key: str, *, transport=None, gate=None):
        if not key or any(c in key for c in "\r\n"):
            raise ValueError("A valid provider credential file is required")
        if transport is None and gate is None:
            raise ValueError("Real Lambda requests require the durable account-wide request gate")
        self.client = httpx.Client(
            base_url=BASE_URL,
            headers={
                "Authorization": "Bearer " + key,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        # Production callers supply the account-wide PostgreSQL request gate.
        self.gate = gate or (lambda launch=False: nullcontext())

    def close(self):
        self.client.close()

    def request(self, method, path, *, body=None, params=None):
        allowed = (
            method == "GET"
            and (
                path in {"instances", "instance-types", "images"}
                or re.fullmatch(r"instances/[a-f0-9]{32}", path)
            )
        ) or (
            method == "POST"
            and path in {"instance-operations/launch", "instance-operations/terminate"}
        )
        if not allowed:
            raise ValueError("Provider request paths must stay on the fixed API origin")
        launch = path == "instance-operations/launch"
        try:
            with (
                self.gate(launch=launch),
                self.client.stream(method, path, json=body, params=params) as response,
            ):
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise ValueError("Provider response exceeded its bound")
                import json

                document = json.loads(content)
                if not isinstance(document, dict):
                    raise ValueError("Unexpected provider response")
                if response.status_code >= 400:
                    # Only a known, exact code affects classification. Never echo API text.
                    error = document.get("error")
                    code = error.get("code") if isinstance(error, dict) else None
                    if (
                        method == "GET"
                        and path.startswith("instances/")
                        and response.status_code == 404
                        and code == "global/object-does-not-exist"
                    ):
                        raise ProviderNotFound("The exact provider instance does not exist")
                    if response.status_code == 400 and code == (
                        "instance-operations/launch/insufficient-capacity"
                    ):
                        raise CapacityUnavailable("Lambda reports insufficient capacity")
                    if response.status_code == 429:
                        raise ProviderRateLimited("Lambda request rate limit rejected request")
                    if response.status_code < 500:
                        raise ProviderRejected(
                            f"Lambda rejected request (HTTP {response.status_code})"
                        )
                    raise ValueError("Provider server failure")
                if response.status_code != 200 or not isinstance(document, dict):
                    raise ValueError("Unexpected provider response")
                return document["data"], document.get("page_token")
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            # A timeout, 5xx, invalid body or redirect can follow a committed launch.
            error = AllocationAmbiguous if launch else TransientProviderError
            raise error("Lambda response unavailable; reconcile durable intent") from None

    def instances(self) -> list[dict]:
        result, seen, token = [], set(), None
        for _ in range(32):
            data, following = self.request(
                "GET",
                "instances",
                params={"page_size": 100, **({"page_token": token} if token else {})},
            )
            if not isinstance(data, list) or any(not isinstance(i, dict) for i in data):
                raise TransientProviderError("Lambda instance list is malformed")
            result.extend(data)
            if not following:
                identities = [i.get("id") for i in result]
                if any(not isinstance(i, str) or not i for i in identities) or len(
                    set(identities)
                ) != len(identities):
                    raise TransientProviderError("Lambda pagination duplicated or omitted identity")
                try:
                    return [safe_instance(i) for i in result]
                except ValueError:
                    raise TransientProviderError("Malformed provider instance metadata") from None
            if not isinstance(following, str) or following in seen:
                raise TransientProviderError("Lambda pagination did not advance")
            seen.add(following)
            token = following
        raise TransientProviderError("Lambda instance-list page bound exceeded")

    def launch(self, plan: LaunchPlan, user_data: str) -> tuple[str, ...]:
        data, _ = self.request("POST", "instance-operations/launch", body=plan.request(user_data))
        ids = data.get("instance_ids") if isinstance(data, dict) else None
        if (
            not isinstance(ids, list)
            or len(ids) != 1
            or not isinstance(ids[0], str)
            or not re.fullmatch(r"[a-f0-9]{32}", ids[0])
        ):
            raise AllocationAmbiguous(
                "Lambda allocation response did not identify exactly one instance"
            )
        return tuple(ids)

    def instance(self, instance_id: str):
        if not re.fullmatch(r"[a-f0-9]{32}", instance_id):
            raise ValueError("Invalid provider instance identity")
        try:
            data, _ = self.request("GET", "instances/" + instance_id)
        except ProviderNotFound:
            return None
        if not isinstance(data, dict) or data.get("id") != instance_id:
            raise ProviderIdentityConflict("Specific instance read changed the requested identity")
        return safe_instance(data)

    def check_offering(self, plan, price_ceiling, *, image_version=None):
        data, _ = self.request("GET", "instance-types")
        candidates = (
            [
                v
                for v in data.values()
                if isinstance(v, dict)
                and isinstance(v.get("instance_type"), dict)
                and v["instance_type"].get("name") == plan.instance_type
            ]
            if isinstance(data, dict)
            else []
        )
        if len(candidates) != 1:
            raise ProviderIdentityConflict("Pinned provider offering is unavailable or ambiguous")
        selected = candidates[0]
        offering = selected["instance_type"]
        price = offering.get("price_cents_per_hour")
        if (
            type(price) is not int
            or not 0 < price <= price_ceiling
            or offering.get("architecture") != "x86_64"
            or type(offering.get("specs", {}).get("gpus")) is not int
            or offering.get("specs", {}).get("gpus") != 1
        ):
            raise ProviderIdentityConflict("Offering price or hardware exceeds the reviewed policy")
        regions = selected.get("regions_with_capacity_available")
        if not isinstance(regions, list) or not any(
            isinstance(r, dict) and r.get("name") == plan.region for r in regions
        ):
            raise CapacityUnavailable("The pinned Virginia A10 has no reported capacity")
        images, seen, token = [], set(), None
        for _ in range(32):
            data, following = self.request(
                "GET", "images", params={"page_token": token} if token else None
            )
            if not isinstance(data, list) or any(not isinstance(i, dict) for i in data):
                raise TransientProviderError("Provider image catalog is malformed")
            images.extend(
                i
                for i in data
                if i.get("id") == plan.image_id
                and isinstance(i.get("region"), dict)
                and i["region"].get("name") == plan.region
            )
            if not following:
                break
            if not isinstance(following, str) or following in seen:
                raise TransientProviderError("Provider image pagination did not advance")
            seen.add(following)
            token = following
        else:
            raise TransientProviderError("Provider image catalog page bound exceeded")
        if (
            len(images) != 1
            or images[0].get("family") != "ubuntu-24-04"
            or images[0].get("architecture") != "x86_64"
            or (image_version is not None and images[0].get("version") != image_version)
        ):
            raise ProviderIdentityConflict(
                "Pinned plain Ubuntu image is unavailable in the selected region"
            )
        return {
            "price_cents_per_hour": price,
            "instance_type": plan.instance_type,
            "region": plan.region,
            "image": {
                k: images[0][k]
                for k in ("id", "family", "architecture", "version")
                if k in images[0]
            },
        }

    def terminate(self, instance_id: str):
        if not re.fullmatch(r"[a-f0-9]{32}", instance_id):
            raise ValueError("Invalid provider instance identity")
        data, _ = self.request(
            "POST", "instance-operations/terminate", body={"instance_ids": [instance_id]}
        )
        if not isinstance(data, dict) or not isinstance(data.get("terminated_instances"), list):
            raise TransientProviderError("Termination response needs reconciliation")
        try:
            instances = [safe_instance(i) for i in data["terminated_instances"]]
        except (ValueError, TypeError, AttributeError):
            raise TransientProviderError("Malformed termination identity") from None
        if len(instances) != 1 or instances[0].get("id") != instance_id:
            raise ProviderIdentityConflict("Termination response differs from requested identity")
        return instances


def owned_instances(plan: LaunchPlan, instances: list[dict], known_ids=()) -> list[dict]:
    """Adopt only an exact launch identity; names or tags alone are insufficient."""
    selected = []
    for instance in instances:
        tags = instance.get("tags", [])
        if not isinstance(tags, list):
            raise ProviderIdentityConflict("Malformed instance ownership tags")
        tag_pairs = [(t.get("key"), t.get("value")) for t in tags if isinstance(t, dict)]
        relevant = (
            instance.get("id") in known_ids
            or instance.get("name") == plan.name
            or ("klip-operation", plan.intent_id) in tag_pairs
        )
        if not relevant:
            continue
        if len({k for k, _ in tag_pairs}) != len(tag_pairs):
            raise ProviderIdentityConflict("Duplicate instance ownership tag")
        if (
            instance.get("name") != plan.name
            or any(dict(tag_pairs).get(k) != v for k, v in plan.tags.items())
            or instance.get("instance_type", {}).get("name") != plan.instance_type
            or instance.get("region", {}).get("name") != plan.region
            or not isinstance(instance.get("id"), str)
        ):
            raise ProviderIdentityConflict("Provider observation differs from allocation intent")
        if instance.get("image") and instance["image"].get("id") != plan.image_id:
            raise ProviderIdentityConflict("Provider image differs from allocation intent")
        if instance.get("ssh_key_names") != [plan.ssh_key_name]:
            raise ProviderIdentityConflict("Provider SSH identity differs from allocation intent")
        selected.append(safe_instance(instance))
    if len(selected) > 1 or (known_ids and any(i["id"] not in known_ids for i in selected)):
        raise ProviderIdentityConflict("Allocation identity is ambiguous; operator review required")
    return selected


def reconcile_launch(state: str, plan: LaunchPlan, instances: list[dict], known_ids=()):
    observed = owned_instances(plan, instances, known_ids)
    if observed:
        return observed
    if state == "pending" and not known_ids:
        return []  # Durable proof that POST has never been attempted.
    # A list is not a strongly consistent history of attempted allocations.
    return None
