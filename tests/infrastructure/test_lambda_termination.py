"""Provider fence confirmation is distinct from a successful termination POST."""

from datetime import UTC, datetime
from unittest.mock import Mock

import httpx
import pytest
from platform_contracts import ProviderFence
from platform_management.providers.base import TransientProviderError
from platform_management.providers.lambda_ledger import terminate_once
from platform_management.providers.lambda_mutations import (
    LambdaMutations,
    ProviderIdentityConflict,
    ProviderRejected,
)
from test_lambda_mutations import INSTANCE, instance
from test_lambda_mutations import plan as shared_plan

plan = shared_plan


def ledger(plan):
    result = Mock()
    result.start_termination.return_value = {
        "state": "terminating",
        "instance_ids": [INSTANCE],
        "termination_requested_at": datetime.now(UTC).isoformat(),
    }
    result.fenced.side_effect = lambda _, evidence: ProviderFence(
        intent_id=plan.intent_id,
        provider_instance_id=INSTANCE,
        termination_requested_at=datetime.now(UTC),
        confirmed_at=datetime.now(UTC),
        observation_sha256="b" * 64,
    )
    return result


def test_post_success_does_not_fence_and_reads_must_later_confirm(plan):
    store, api = ledger(plan), Mock()
    observed = instance(plan)
    api.instances.return_value = [observed]
    api.instance.return_value = observed
    api.terminate.return_value = [{**observed, "status": "terminating"}]
    assert terminate_once(plan, store, api) is None
    store.termination_acknowledged.assert_called_once()
    store.fenced.assert_not_called()
    api.instances.return_value, api.instance.return_value = [], None
    receipt = terminate_once(plan, store, api)
    assert receipt.provider_instance_id == INSTANCE
    api.terminate.assert_called_once_with(INSTANCE)


@pytest.mark.parametrize("absent", ["list", "specific"])
def test_inconsistent_reads_remain_unfenced_without_another_post(plan, absent):
    store, api = ledger(plan), Mock()
    observed = instance(plan)
    api.instances.return_value = [] if absent == "list" else [observed]
    api.instance.return_value = None if absent == "specific" else observed
    assert terminate_once(plan, store, api) is None
    store.fenced.assert_not_called()
    api.terminate.assert_not_called()


def test_terminal_receipt_is_reused_without_api_or_credential_access(plan):
    store, api = ledger(plan), Mock()
    receipt = store.fenced(plan, {})
    store.start_termination.return_value["provider_fence"] = receipt.model_dump(mode="json")
    assert terminate_once(plan, store, api) == receipt
    assert not api.mock_calls


def test_changed_provider_ownership_never_terminates(plan):
    store, api = ledger(plan), Mock()
    observed = instance(plan)
    api.instances.return_value = [observed]
    api.instance.return_value = {**observed, "name": "someone-else"}
    with pytest.raises(ProviderIdentityConflict):
        terminate_once(plan, store, api)
    api.terminate.assert_not_called()


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (404, "global/object-does-not-exist", None),
        (404, "global/invalid-parameters", ProviderRejected),
        (401, "global/invalid-api-key", ProviderRejected),
        (500, "server-error", TransientProviderError),
    ],
)
def test_only_documented_specific_404_means_absence(status, code, expected):
    api = LambdaMutations(
        "fixture",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, json={"error": {"code": code}})
        ),
    )
    try:
        if expected:
            with pytest.raises(expected):
                api.instance(INSTANCE)
        else:
            assert api.instance(INSTANCE) is None
    finally:
        api.close()


def test_specific_instance_lookup_refuses_paths_and_changed_ids():
    calls = []

    def response(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"data": {"id": "b" * 32}})

    api = LambdaMutations("fixture", transport=httpx.MockTransport(response))
    try:
        with pytest.raises(ValueError):
            api.instance("../other")
        assert not calls
        with pytest.raises(ProviderIdentityConflict):
            api.instance(INSTANCE)
        assert calls == ["https://cloud.lambda.ai/api/v1/instances/" + INSTANCE]
    finally:
        api.close()
