"""Cloud-free proof of the real-provider HTTP and ownership boundary."""

import copy
import hashlib
import json
from contextlib import contextmanager

import httpx
import pytest
from platform_management.providers.base import (
    AllocationAmbiguous,
    CapacityUnavailable,
    TransientProviderError,
)
from platform_management.providers.lambda_mutations import (
    MAX_RESPONSE_BYTES,
    LambdaMutations,
    LaunchPlan,
    ProviderIdentityConflict,
    ProviderRejected,
    owned_instances,
    reconcile_launch,
)

USER_DATA = "#cloud-config\nruncmd: []\n"
INSTANCE = "a" * 32


@pytest.fixture
def plan():
    return LaunchPlan(
        intent_id="op-" + "1" * 16,
        instance_type="gpu_1x_a10",
        region="us-east-1",
        image_id="synthetic-ubuntu-image",
        ssh_key_name="synthetic-executor-key",
        firewall_ruleset_ids=("synthetic-firewall",),
        user_data_sha256=hashlib.sha256(USER_DATA.encode()).hexdigest(),
    )


def instance(plan):
    return {
        "id": INSTANCE,
        "name": plan.name,
        "tags": [{"key": k, "value": v} for k, v in plan.tags.items()],
        "instance_type": {"name": plan.instance_type},
        "region": {"name": plan.region},
        "image": {"id": plan.image_id},
        "ssh_key_names": [plan.ssh_key_name],
        "status": "active",
        "jupyter_token": "synthetic-secret-must-never-be-retained",
        "jupyter_url": "https://example.invalid/?token=synthetic-secret",
    }


def client(handler, gate=None):
    return LambdaMutations(
        "synthetic-provider-secret", transport=httpx.MockTransport(handler), gate=gate
    )


def test_launch_preserves_pins_and_single_attempt_without_leaking_credentials(plan):
    seen, gates = [], []

    @contextmanager
    def gate(*, launch):
        gates.append(launch)
        yield

    def handler(request):
        seen.append(request)
        assert str(request.url) == "https://cloud.lambda.ai/api/v1/instance-operations/launch"
        assert request.headers["Authorization"] == "Bearer synthetic-provider-secret"
        body = json.loads(request.content)
        assert body["image"] == {"id": plan.image_id}
        assert body["quantity"] == 1
        assert body["user_data"] == USER_DATA
        assert body["ssh_key_names"] == [plan.ssh_key_name]
        assert body["firewall_rulesets"] == [{"id": "synthetic-firewall"}]
        return httpx.Response(200, json={"data": {"instance_ids": [INSTANCE]}})

    api = client(handler, gate)
    try:
        assert api.launch(plan, USER_DATA) == (INSTANCE,)
        with pytest.raises(ValueError, match="digest"):
            api.launch(plan, USER_DATA + "# changed")
        assert len(seen) == 1 and gates == [True]
    finally:
        api.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, json={"error": {"message": "synthetic-provider-secret"}}),
        httpx.Response(200, text="synthetic-provider-secret"),
        httpx.Response(200, json={"data": {"instance_ids": []}}),
        httpx.Response(200, json={"data": {"instance_ids": [INSTANCE, "b" * 32]}}),
        httpx.Response(200, json=["synthetic-provider-secret"]),
        httpx.Response(302, headers={"Location": "https://attacker.invalid/"}),
        httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)),
    ],
)
def test_ambiguous_post_is_never_retried_or_redirected(plan, response):
    calls = []

    def handler(request):
        calls.append(request)
        return response

    api = client(handler)
    try:
        with pytest.raises(AllocationAmbiguous) as error:
            api.launch(plan, USER_DATA)
        assert len(calls) == 1
        assert "synthetic-provider-secret" not in str(error.value)
        assert error.value.__cause__ is None
    finally:
        api.close()


def test_timeout_after_commit_remains_ambiguous(plan):
    def handler(request):
        raise httpx.ReadTimeout("synthetic-provider-secret", request=request)

    api = client(handler)
    try:
        with pytest.raises(AllocationAmbiguous, match="reconcile"):
            api.launch(plan, USER_DATA)
    finally:
        api.close()


@pytest.mark.parametrize(
    "status,code,error",
    [
        (400, "instance-operations/launch/insufficient-capacity", CapacityUnavailable),
        (429, "global/rate-limited", TransientProviderError),
        (403, "global/account-inactive", ProviderRejected),
        (400, "global/invalid-parameters", ProviderRejected),
    ],
)
def test_definite_rejections_have_safe_classification(plan, status, code, error):
    api = client(
        lambda _: httpx.Response(status, json={"error": {"code": code, "message": "secret"}})
    )
    try:
        with pytest.raises(error) as caught:
            api.launch(plan, USER_DATA)
        assert "secret" not in str(caught.value)
    finally:
        api.close()


def test_paginated_instances_are_bounded_and_secret_fields_are_removed(plan):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"data": [instance(plan)], "page_token": "opaque-next"})
        assert request.url.params["page_token"] == "opaque-next"
        return httpx.Response(200, json={"data": [], "page_token": None})

    api = client(handler)
    try:
        items = api.instances()
        assert len(items) == 1 and items[0]["id"] == INSTANCE
        assert "jupyter" not in json.dumps(items) and "secret" not in json.dumps(items)
    finally:
        api.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [], "page_token": "same"},
        {"data": [{"id": INSTANCE}, {"id": INSTANCE}]},
        {"data": {"not": "a list"}},
        {"data": [{}]},
    ],
)
def test_incomplete_or_duplicate_pagination_cannot_confirm_absence(payload):
    api = client(lambda _: httpx.Response(200, json=payload))
    try:
        with pytest.raises(TransientProviderError):
            api.instances()
    finally:
        api.close()


def test_absence_only_authorizes_the_first_unattempted_launch(plan):
    assert reconcile_launch("pending", plan, []) == []
    for state in ("dispatching", "allocated", "rejected", "termination_requested"):
        assert reconcile_launch(state, plan, []) is None
    assert reconcile_launch("pending", plan, [], (INSTANCE,)) is None
    found = reconcile_launch("dispatching", plan, [instance(plan)])
    assert found[0]["id"] == INSTANCE and "jupyter_token" not in found[0]


@pytest.mark.parametrize(
    "change",
    [
        {"name": "another-owned-name"},
        {"tags": [{"key": "klip-operation", "value": "other"}]},
        {"instance_type": {"name": "gpu_8x_a100"}},
        {"region": {"name": "us-west-1"}},
        {"image": {"id": "different-image"}},
        {"ssh_key_names": ["different-key"]},
    ],
)
def test_matching_id_with_changed_ownership_or_hardware_is_not_adopted(plan, change):
    value = {**instance(plan), **change}
    with pytest.raises(ProviderIdentityConflict):
        owned_instances(plan, [value], (INSTANCE,))


def test_duplicate_allocations_and_duplicate_tags_require_review(plan):
    first = instance(plan)
    second = {**first, "id": "b" * 32}
    with pytest.raises(ProviderIdentityConflict):
        owned_instances(plan, [first, second])
    first = copy.deepcopy(first)
    first["tags"].append(first["tags"][0])
    with pytest.raises(ProviderIdentityConflict):
        owned_instances(plan, [first])


def test_other_resources_are_not_adopted_or_modified(plan):
    assert owned_instances(plan, [{"id": "foreign", "name": "foreign"}]) == []
    api = client(lambda _: pytest.fail("No request should be sent"))
    try:
        for path in ("https://attacker.invalid/", "../api-keys", "api-keys", "instances?x=1"):
            with pytest.raises(ValueError):
                api.request("GET", path)
        with pytest.raises(ValueError):
            api.terminate("../../foreign")
    finally:
        api.close()


def test_termination_ack_is_not_treated_as_a_different_resource(plan):
    api = client(
        lambda _: httpx.Response(
            200,
            json={
                "data": {
                    "terminated_instances": [
                        {**instance(plan), "id": "b" * 32, "status": "terminated"}
                    ]
                }
            },
        )
    )
    try:
        with pytest.raises(ProviderIdentityConflict):
            api.terminate(INSTANCE)
    finally:
        api.close()
