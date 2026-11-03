import copy

import httpx
import pytest
from platform_contracts import ExecutionTarget, RunSnapshot, validate_for_execution
from workload_runner.control import target_readiness
from workload_runner.datasets.preparation import prepare_customer
from workload_runner.scenario_loader import compile_run, default_bindings


def test_direct_vllm_only_omits_llmd_requirement(compile_scenario):
    snapshot = compile_scenario()
    snapshot.target.serving_stack = "direct-vllm"
    with pytest.raises(ValueError) as error:
        validate_for_execution(snapshot)
    assert "llm-d version" not in str(error.value)
    assert "vllm version" in str(error.value)
    assert "unresolved runner image" in str(error.value)
    snapshot.target.serving_stack = "llm-d-vllm"
    with pytest.raises(ValueError, match="llm-d version"):
        validate_for_execution(snapshot)


def test_ambiguous_alias_requires_binding(catalog):
    profile = next(iter(catalog.records["serving-profile"].values()))
    other = profile.model_copy(update={"revision": 2})
    catalog.add("serving-profile", other)
    with pytest.raises(ValueError, match="ambiguous"):
        default_bindings(catalog, "platform-mixed@1")


def test_sustained_targets_and_unique_samples(catalog, tmp_path):
    prepared = {
        "writer-assist@1": prepare_customer(
            catalog, "writer-assist@1", tmp_path / "data", limit=1000
        )
    }
    before = copy.deepcopy(catalog.get("scenario", "writer-assist-baseline@1").model_dump())
    target = ExecutionTarget(
        mode="local-stub", endpoint="http://localhost:8001/v1/chat/completions"
    )
    snapshot = compile_run(
        catalog, "writer-assist-baseline@1", prepared, target, tmp_path / "plan", sustained=True
    )
    assert snapshot.scenario.measurement_duration_s == 1200
    assert snapshot.customers[0].slo == catalog.get("customer", "writer-assist@1").slo
    assert snapshot.scenario.warmup.duration_s == 60 and snapshot.scenario.drain_timeout_s == 120
    assert "-sustained-" in snapshot.scenario.id
    assert catalog.get("scenario", "writer-assist-baseline@1").model_dump() == before
    from workload_runner.artifacts import artifact_path, read_jsonl

    rows = list(read_jsonl(artifact_path(snapshot.admission_schedule.uri)))
    assert len({r["entry_id"] for r in rows}) == len(rows)
    with pytest.raises(ValueError, match="exhaustion"):
        small = {
            "writer-assist@1": prepare_customer(
                catalog, "writer-assist@1", tmp_path / "small", limit=100
            )
        }
        compile_run(
            catalog, "writer-assist-baseline@1", small, target, tmp_path / "bad", sustained=True
        )


def test_template_mismatch_cannot_enter_snapshot(compile_scenario):
    snapshot = compile_scenario()
    data = snapshot.model_dump(mode="json")
    data["serving_profiles"][0]["engine"]["chat_template_kwargs"] = {"enable_thinking": False}
    with pytest.raises(ValueError, match="chat-template"):
        RunSnapshot.model_validate(data)


@pytest.mark.parametrize("stack", ["direct-vllm", "llm-d-vllm"])
def test_live_readiness_uses_bounded_unscored_probes(stack, compile_scenario, monkeypatch):
    snapshot = compile_scenario()
    snapshot.target.mode, snapshot.target.serving_stack = "benchmark", stack
    snapshot.serving_profiles[0].engine.chat_template_kwargs = {"enable_thinking": False}
    calls = []

    def handler(request):
        import json

        calls.append(request.method)
        if request.method == "GET":
            assert stack == "direct-vllm"
            return httpx.Response(200, json={"data": [{"id": snapshot.customers[0].model_alias}]})
        payload = json.loads(request.content)
        assert payload["max_tokens"] == 1 and payload["stream"] is False
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    factory = httpx.Client
    monkeypatch.setattr(
        "workload_runner.control.httpx.Client",
        lambda **kwargs: factory(**kwargs, transport=httpx.MockTransport(handler)),
    )
    assert target_readiness(snapshot)[0]["customer_ref"] == snapshot.customers[0].ref
    assert calls == (["GET", "POST"] if stack == "direct-vllm" else ["POST"])


@pytest.mark.parametrize("stack", ["direct-vllm", "llm-d-vllm"])
def test_benchmark_readiness_rejects_fixture_endpoints(stack, compile_scenario, monkeypatch):
    snapshot = compile_scenario()
    snapshot.target.mode, snapshot.target.serving_stack = "benchmark", stack
    factory = httpx.Client
    monkeypatch.setattr(
        "workload_runner.control.httpx.Client",
        lambda **kwargs: factory(
            **kwargs,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"fixture": True})),
        ),
    )
    with pytest.raises(ValueError, match="fixture endpoint"):
        target_readiness(snapshot)
