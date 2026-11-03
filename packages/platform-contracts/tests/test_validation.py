import hashlib
import json
from copy import deepcopy

import pytest
from platform_contracts import (
    CustomerSpec,
    InfrastructureInventory,
    OperationStatus,
    Run,
    RunSnapshot,
    ScenarioSpec,
    ServingProfile,
    WorkerGroup,
    build_run_snapshot,
    load_catalog,
    load_document,
    validate_for_execution,
)
from platform_contracts.common import Artifact
from platform_contracts.runs import ExecutionVersions


def mutate_snapshot(snapshot, mutation):
    data = snapshot.model_dump(mode="json")
    mutation(data)
    return RunSnapshot.model_validate(data)


def test_snapshot_is_an_independent_default_filled_copy(catalog, mixed_snapshot):
    snapshot = build_run_snapshot(catalog, "platform-mixed@1", mixed_snapshot.bindings)
    before = snapshot.model_dump_json()
    catalog.get("customer", "writer-assist@1").generation.temperature = 0.5
    catalog.get("scenario", "platform-mixed@1").phases[0].arrivals["writer-assist"].rate = 0.2
    assert snapshot.model_dump_json() == before
    assert snapshot.customers[0] is not catalog.get("customer", snapshot.customers[0].ref)
    assert snapshot.versions.platform_versions == {}
    assert snapshot.serving_profiles[0].engine.backend == "vllm"


def test_snapshot_copies_inventory_versions_and_admission_artifact(catalog, mixed_snapshot, root):
    inventory = InfrastructureInventory.model_validate_json(
        (root / "contracts/fixtures/inventories/cluster.json").read_text()
    )
    versions = ExecutionVersions(platform_versions={"vllm": "fixture-version"})
    artifact = Artifact(uri="fixture://schedule.jsonl", sha256="a" * 64)
    snapshot = build_run_snapshot(
        catalog,
        "platform-mixed@1",
        mixed_snapshot.bindings,
        inventory=inventory,
        versions=versions,
        admission_schedule=artifact,
    )
    before = snapshot.model_dump_json()
    inventory.nodes[0].readiness.host = False
    versions.platform_versions["vllm"] = "changed"
    artifact.sha256 = "b" * 64
    assert snapshot.model_dump_json() == before


def test_duplicate_catalog_reference_is_rejected(catalog):
    with pytest.raises(ValueError, match="duplicate customer reference"):
        catalog.add("customer", catalog.get("customer", "writer-assist@1"))


def test_catalog_rejects_wrong_contract_kind(catalog):
    with pytest.raises(ValueError, match="incorrect contract type"):
        catalog.add("scenario", catalog.get("customer", "writer-assist@1"))


def test_missing_revision_is_not_silently_replaced(catalog):
    with pytest.raises(ValueError, match="missing customer reference"):
        catalog.get("customer", "writer-assist@2")


def test_missing_repository_configuration_is_reported(tmp_path):
    with pytest.raises(ValueError, match="missing configuration files"):
        load_catalog(tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        "id: writer-assist\nid: other\n",
        "!!python/object/apply:os.system ['exit 1']",
        "? [invalid, mapping, key]\n: value\n",
    ],
)
def test_yaml_rejects_duplicate_keys_and_unsafe_constructors(tmp_path, text):
    path = tmp_path / "bad.yaml"
    path.write_text(text)
    with pytest.raises(ValueError):
        load_document(path, CustomerSpec)


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", 0),
        ("revision", True),
        ("revision", 1.5),
        ("id", "Bad Name"),
        ("schema_version", "9.0"),
        ("unknown_setting", 1),
    ],
)
def test_invalid_common_fields_are_rejected(catalog, field, value):
    data = catalog.get("customer", "writer-assist@1").model_dump(mode="json")
    data[field] = value
    with pytest.raises(ValueError):
        CustomerSpec.model_validate(data)


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf"), True])
def test_invalid_arrival_rates_are_rejected(catalog, value):
    data = catalog.get("scenario", "writer-assist-baseline@1").model_dump(mode="json")
    data["phases"][0]["arrivals"]["writer-assist"]["rate"] = value
    with pytest.raises(ValueError):
        ScenarioSpec.model_validate(data)


def test_duplicate_participant_identity_rejected(catalog):
    data = catalog.get("scenario", "writer-assist-baseline@1").model_dump(mode="json")
    data["participants"].append("writer-assist@2")
    with pytest.raises(ValueError, match="duplicate customer identities"):
        ScenarioSpec.model_validate(data)


def test_missing_binding_rejected(mixed_snapshot):
    with pytest.raises(ValueError, match="exactly one serving binding"):
        mutate_snapshot(mixed_snapshot, lambda d: d["bindings"].pop())


def test_duplicate_snapshot_revision_rejected(mixed_snapshot):
    with pytest.raises(ValueError, match="duplicate customer reference"):
        mutate_snapshot(mixed_snapshot, lambda d: d["customers"].append(d["customers"][0]))


def test_missing_dataset_reference_rejected(mixed_snapshot):
    with pytest.raises(ValueError, match="missing dataset-manifest"):
        mutate_snapshot(mixed_snapshot, lambda d: d.update(datasets=[]))


def test_model_alias_mismatch_rejected(mixed_snapshot):
    with pytest.raises(ValueError, match="model alias"):
        mutate_snapshot(
            mixed_snapshot, lambda d: d["customers"][0].update(model_alias="wrong-model")
        )


def test_context_overflow_rejected(mixed_snapshot):
    def change(data):
        customer = next(c for c in data["customers"] if c["id"] == "writer-assist")
        customer["limits"]["max_rendered_input_tokens"] = 768

    with pytest.raises(ValueError, match="exceeds serving context"):
        mutate_snapshot(mixed_snapshot, change)


def test_gpu_memory_mismatch_rejected(mixed_snapshot):
    with pytest.raises(ValueError, match="memory is below"):
        mutate_snapshot(
            mixed_snapshot, lambda d: d["worker_groups"][0]["hardware"].update(vram_gib_per_gpu=1.0)
        )


def test_shared_deployment_is_not_double_counted(mixed_snapshot):
    RunSnapshot.model_validate_json(mixed_snapshot.model_dump_json())

    def change(data):
        binding = next(b for b in data["bindings"] if b["customer_ref"] == "research-agent@1")
        binding["deployment_id"] = "second-medium-deployment"

    with pytest.raises(ValueError, match="aggregate serving GPU allocation"):
        mutate_snapshot(mixed_snapshot, change)


def test_incompatible_deployment_identity_rejected(mixed_snapshot):
    def change(data):
        binding = next(b for b in data["bindings"] if b["customer_ref"] == "research-agent@1")
        binding["deployment_id"] = "shared-small"

    with pytest.raises(ValueError, match="deployment identity"):
        mutate_snapshot(mixed_snapshot, change)


def test_dedicated_group_cannot_be_shared(mixed_snapshot):
    def change(data):
        binding = next(b for b in data["bindings"] if b["customer_ref"] == "writer-assist@1")
        binding["worker_group_ref"] = "shared-workers@1"
        profile = next(p for p in data["serving_profiles"] if p["id"] == "gec-baseline")
        profile["worker_class"] = "shared"
        profile["hardware"]["gpu_model"] = "H100"
        data["worker_groups"] = [g for g in data["worker_groups"] if g["id"] != "latency-workers"]

    with pytest.raises(ValueError, match="exclusive worker group"):
        mutate_snapshot(mixed_snapshot, change)


def test_provisioned_reservations_fit_serving_concurrency(mixed_snapshot):
    def change(data):
        customer = next(c for c in data["customers"] if c["id"] == "research-agent")
        customer["reserved_concurrency"] = 17
        customer["limits"]["max_inflight_requests"] = 20

    with pytest.raises(ValueError, match="reservations exceed"):
        mutate_snapshot(mixed_snapshot, change)


@pytest.mark.parametrize("mode", ["wrong_unit", "excess_rate", "excess_duration"])
def test_scenario_must_fit_customer_allowance(mixed_snapshot, mode):
    def change(data):
        arrival = data["scenario"]["phases"][1]["arrivals"]["writer-assist"]
        if mode == "wrong_unit":
            arrival["unit"] = "session"
        elif mode == "excess_rate":
            arrival["rate"] = 10.0
        else:
            data["scenario"]["phases"][1]["duration_s"] = 61

    with pytest.raises(ValueError, match="allowance"):
        mutate_snapshot(mixed_snapshot, change)


def test_splitting_a_burst_does_not_bypass_duration_allowance(mixed_snapshot):
    def change(data):
        burst = data["scenario"]["phases"][1]
        burst["duration_s"] = 40
        second = deepcopy(burst)
        second["id"] = "continued-burst"
        data["scenario"]["phases"].insert(2, second)

    with pytest.raises(ValueError, match="consecutive burst duration"):
        mutate_snapshot(mixed_snapshot, change)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["result"]["requests"].update(completed=201),
        lambda d: d["result"]["requests"].update(eligible_for_slo=201),
        lambda d: d["result"]["slo_assessments"][0]["windows"][0].update(good_requests=101),
        lambda d: d["result"]["slo_assessments"][0]["windows"][0].update(eligible_requests=2),
        lambda d: d["result"]["slo_assessments"][0].update(threshold_ms=5000.0),
        lambda d: d["result"].update(validity="invalid", invalid_reasons=["missing metrics"]),
        lambda d: d["result"]["slo_assessments"][0]["windows"].pop(),
    ],
)
def test_invalid_counts_targets_windows_and_verdicts_are_rejected(writer_run_data, mutation):
    mutation(writer_run_data)
    with pytest.raises(ValueError):
        Run.model_validate(writer_run_data)


def test_insufficient_sample_cannot_be_passing(writer_run_data):
    result = writer_run_data["result"]
    for window in result["slo_assessments"][0]["windows"]:
        window.update(eligible_requests=5, good_requests=5)
    with pytest.raises(ValueError, match="verdict disagrees"):
        Run.model_validate(writer_run_data)


def test_slo_cannot_omit_eligible_customer_requests(root):
    data = json.loads(
        (root / "contracts/fixtures/runs/tutor-chat/completed-passing.json").read_text()
    )
    for window in data["result"]["slo_assessments"][0]["windows"]:
        window.update(eligible_requests=50, good_requests=49)
    with pytest.raises(ValueError, match="all eligible customer requests"):
        Run.model_validate(data)


def test_aggregate_and_customer_outcomes_must_reconcile(writer_run_data):
    counts = writer_run_data["result"]["customer_requests"]["writer-assist@1"]
    counts.update(completed=195, failed=2)
    with pytest.raises(ValueError, match="per-customer counts"):
        Run.model_validate(writer_run_data)


def test_code_replay_cannot_claim_agent_success(root):
    data = json.loads(
        (root / "contracts/fixtures/runs/code-agent/completed-passing.json").read_text()
    )
    data["result"]["quality"][0].update(status="measured", scores={"agent-success": 1.0})
    with pytest.raises(ValueError, match="functional agent quality"):
        Run.model_validate(data)


def test_incomplete_admission_schedule_cannot_have_valid_measurements(writer_run_data):
    counts = writer_run_data["result"]["requests"]
    counts.update(offered=210, not_sent=10)
    writer_run_data["result"]["customer_requests"]["writer-assist@1"].update(counts)
    with pytest.raises(ValueError, match="incomplete offered schedule"):
        Run.model_validate(writer_run_data)


def test_non_streaming_latency_is_absent_rather_than_zero(writer_run_data):
    summary = writer_run_data["result"]["measurements"][0]["latencies"]["completion_latency_ms"]
    writer_run_data["result"]["measurements"][0]["latencies"]["ttft_ms"] = summary
    with pytest.raises(ValueError, match="non-streaming"):
        Run.model_validate(writer_run_data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("started_at", "2026-11-03T11:00:00Z"),
        ("finished_at", "2026-11-03T11:00:00Z"),
        ("created_at", "2026-11-03T12:00:00"),
        ("created_at", 0),
        ("created_at", "0"),
        ("status", "queued"),
    ],
)
def test_invalid_execution_timestamps_rejected(writer_run_data, field, value):
    writer_run_data[field] = value
    with pytest.raises(ValueError):
        Run.model_validate(writer_run_data)


def test_aware_timestamps_normalize_to_utc(writer_run_data):
    writer_run_data["created_at"] = "2026-11-03T13:00:00+01:00"
    assert Run.model_validate(writer_run_data).created_at.isoformat() == "2026-11-03T12:00:00+00:00"


def test_ready_profile_requires_pins(catalog):
    data = catalog.get("serving-profile", "gec-baseline@1").model_dump(mode="json")
    data["draft"] = False
    with pytest.raises(ValueError, match="require image and model pins"):
        ServingProfile.model_validate(data)


def test_optional_speculation_configuration_is_typed(catalog):
    data = catalog.get("serving-profile", "gec-baseline@1").model_dump(mode="json")
    data["engine"]["speculative_decoding"] = {"method": "ngram"}
    assert ServingProfile.model_validate(data).engine.speculative_decoding.method == "ngram"
    data["engine"]["speculative_decoding"]["prompt_lookup_min"] = 10
    with pytest.raises(ValueError, match="lookup minimum"):
        ServingProfile.model_validate(data)


def test_inventory_rejects_cidr_overlap(root):
    data = json.loads((root / "contracts/fixtures/inventories/cluster.json").read_text())
    data["network"]["pod_cidr"] = "10.200.0.0/16"
    with pytest.raises(ValueError, match="must not overlap"):
        InfrastructureInventory.model_validate(data)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["nodes"].append(d["nodes"][0]),
        lambda d: d["nodes"][0].update(wireguard_ip="192.0.2.1"),
        lambda d: d["nodes"][0]["access"].update(private_key="not-allowed"),
        lambda d: d["nodes"][0].update(observed_at="2026-11-04T12:00:00Z"),
        lambda d: d["nodes"][3]["kubernetes"].update(allocated_gpu_count=100),
    ],
)
def test_invalid_inventory_observations_rejected(root, mutation):
    data = json.loads((root / "contracts/fixtures/inventories/cluster.json").read_text())
    mutation(data)
    with pytest.raises(ValueError):
        InfrastructureInventory.model_validate(data)


def test_worker_group_counts_are_checked_against_member_identities(root):
    data = json.loads((root / "contracts/fixtures/worker-groups/ready.json").read_text())
    data["status"]["counts"]["gpu_ready_nodes"] = 0
    with pytest.raises(ValueError, match="counts disagree"):
        WorkerGroup.model_validate(data)


def test_operation_cannot_complete_before_cleanup(root):
    data = json.loads((root / "contracts/fixtures/operations/failed.json").read_text())
    data.update(status="completed", error=None)
    with pytest.raises(ValueError, match="pending cleanup"):
        OperationStatus.model_validate(data)


def test_execution_preflight_rejects_all_unresolved_inputs(mixed_snapshot):
    with pytest.raises(ValueError) as exc:
        validate_for_execution(mixed_snapshot)
    for required in [
        "draft serving profile",
        "missing prepared dataset",
        "instance type/region",
        "runner image",
        "inventory",
        "admission schedule",
    ]:
        assert required in str(exc.value)


@pytest.fixture
def complete_snapshot(catalog, mixed_snapshot, root, tmp_path):
    """Synthetic metadata exercises completeness only; no compatibility or GPU claims."""
    snapshot = build_run_snapshot(
        catalog,
        "writer-assist-baseline@1",
        tuple(b for b in mixed_snapshot.bindings if b.customer_ref == "writer-assist@1"),
    )
    data = snapshot.model_dump(mode="json")
    profile = data["serving_profiles"][0]
    profile["draft"] = False
    profile["engine"]["image"] = "registry.example.com/test/engine@sha256:" + "a" * 64
    profile["model"].update(revision="b" * 40, tokenizer_revision="b" * 40)
    data["worker_groups"][0].update(instance_type="unit-test-sku", region="unit-test-region")
    payload = tmp_path / "synthetic-unit-test.jsonl"
    payload.write_text('{"synthetic": true}\n')
    checksum = hashlib.sha256(payload.read_bytes()).hexdigest()
    for dataset in data["datasets"]:
        dataset["source"].update(revision="unit-test-source-v1", license="unit-test-only")
        dataset["prepared"] = dict(
            uri=payload.as_uri(),
            sha256=checksum,
            record_count=1000,
            tokenizer_revision="b" * 40,
            prompt_template_sha256=snapshot.customers[0].workload.prompt_template_digest,
            max_rendered_input_tokens=100,
        )
    data["inventory"] = json.loads(
        (root / "contracts/fixtures/inventories/cluster.json").read_text()
    )
    data["inventory"]["fixture"] = False
    data["admission_schedule"] = dict(uri=payload.as_uri(), sha256=checksum)
    data["versions"] = dict(
        runner_image="registry.example.com/test/runner@sha256:" + "c" * 64,
        aiperf_version="1.0.0",
        evaluator_version="1.0.0",
        platform_versions={
            key: "1.0.0" for key in ("kubernetes", "cilium", "llm-d", "vllm", "model-pool-chart")
        },
    )
    return RunSnapshot.model_validate(data)


def test_offline_execution_completeness_accepts_resolved_metadata(complete_snapshot):
    validate_for_execution(complete_snapshot)


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda d: d["worker_groups"][0].update(ttl_minutes=5), "lease is shorter"),
        (
            lambda d: d["datasets"][0]["prepared"].update(tokenizer_revision="d" * 40),
            "different tokenizer",
        ),
        (
            lambda d: d["datasets"][0]["prepared"].update(max_rendered_input_tokens=600),
            "input limit",
        ),
        (
            lambda d: d["datasets"][0]["prepared"].update(prompt_template_sha256="a" * 64),
            "prompt template",
        ),
        (lambda d: d["inventory"].update(fixture=True), "non-fixture inventory"),
        (lambda d: d["versions"].update(aiperf_version="latest"), "unresolved aiperf"),
    ],
)
def test_execution_preflight_checks_resolved_metadata(complete_snapshot, mutation, match):
    data = complete_snapshot.model_dump(mode="json")
    mutation(data)
    with pytest.raises(ValueError, match=match):
        validate_for_execution(RunSnapshot.model_validate(data))
