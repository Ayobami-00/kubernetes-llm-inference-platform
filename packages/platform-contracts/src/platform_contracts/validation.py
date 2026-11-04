"""Cross-contract checks use supplied data only, with no infrastructure or tokenizer calls."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import TYPE_CHECKING

from .common import is_commit_pin, is_image_pin

if TYPE_CHECKING:
    from .loading import ContractCatalog
    from .runs import RunResult, RunSnapshot


def _indexed(records, kind):
    result = {}
    for record in records:
        if record.ref in result:
            raise ValueError(f"duplicate {kind} reference: {record.ref}")
        result[record.ref] = record
    return result


def _validate_customer_dataset(customer, datasets):
    dataset = datasets.get(customer.workload.dataset_ref)
    if dataset is None:
        raise ValueError(f"missing dataset-manifest reference: {customer.workload.dataset_ref}")
    if dataset.interaction != customer.workload.interaction:
        raise ValueError(f"{customer.id}: dataset interaction differs from customer")
    if dataset.format == "exgentic_snapshot" and not customer.workload.preserve_recorded_context:
        raise ValueError("Exgentic requires preserved recorded context")


def _validate_scenario(scenario, customers, datasets):
    for ref in scenario.participants:
        if ref not in customers:
            raise ValueError(f"missing customer reference: {ref}")
        customer = customers[ref]
        _validate_customer_dataset(customer, datasets)
        warmup_ref = scenario.warmup.datasets.get(customer.id)
        if warmup_ref:
            if warmup_ref not in datasets:
                raise ValueError(f"missing warmup dataset-manifest reference: {warmup_ref}")
            if datasets[warmup_ref].interaction != customer.workload.interaction:
                raise ValueError("warmup dataset interaction differs from customer")
        allowance = customer.traffic_allowance
        burst_duration = 0
        for phase in scenario.phases:
            arrival = phase.arrivals[customer.id]
            if arrival.unit != allowance.unit:
                raise ValueError(f"{customer.id}: arrival unit differs from allowance")
            rate = arrival.per_second
            if rate > allowance.burst_per_second and not math.isclose(
                rate, allowance.burst_per_second
            ):
                raise ValueError(f"{customer.id}: arrival rate exceeds burst allowance")
            if rate > allowance.sustained_per_second and not math.isclose(
                rate, allowance.sustained_per_second
            ):
                burst_duration += phase.duration_s
                if burst_duration > allowance.max_burst_duration_s:
                    raise ValueError(f"{customer.id}: consecutive burst duration exceeds allowance")
            else:
                burst_duration = 0


def validate_catalog(catalog: ContractCatalog) -> None:
    customers = catalog.records.get("customer", {})
    datasets = catalog.records.get("dataset-manifest", {})
    for customer in customers.values():
        _validate_customer_dataset(customer, datasets)
    for scenario in catalog.records.get("scenario", {}).values():
        _validate_scenario(scenario, customers, datasets)


def validate_snapshot(snapshot: RunSnapshot) -> None:
    customers = _indexed(snapshot.customers, "customer")
    profiles = _indexed(snapshot.serving_profiles, "serving-profile")
    groups = _indexed(snapshot.worker_groups, "worker-group")
    datasets = _indexed(snapshot.datasets, "dataset-manifest")
    if snapshot.target and set(snapshot.target.credential_env) - {c.id for c in snapshot.customers}:
        raise ValueError("credential reference belongs to a customer outside the snapshot")
    if snapshot.preparation:
        preparations = {p.customer_ref: p for p in snapshot.preparation}
        if len(preparations) != len(snapshot.preparation):
            raise ValueError("duplicate preparation customer reference")
        if len({p.id for p in snapshot.preparation}) != len(snapshot.preparation):
            raise ValueError("duplicate preparation identity")
        expected = {snapshot.derivation.get(c.ref) for c in snapshot.customers}
        if expected != set(preparations):
            raise ValueError("preparation references disagree with customer derivation")
        for customer in snapshot.customers:
            prepared = preparations[snapshot.derivation[customer.ref]]
            if customer.workload.dataset_ref != prepared.id + "@1":
                raise ValueError("customer dataset differs from preparation identity")
            dataset = datasets[customer.workload.dataset_ref]
            if (
                dataset.prepared is None
                or dataset.prepared.sha256 != prepared.artifact.sha256
                or dataset.prepared.uri != prepared.artifact.uri
            ):
                raise ValueError("snapshot dataset differs from preparation artifact")
            if prepared.prompt_template_sha256 != customer.workload.prompt_template_digest:
                raise ValueError("preparation prompt template differs from customer")
    if set(customers) != set(snapshot.scenario.participants):
        raise ValueError("snapshot customers differ from scenario participants")
    _validate_scenario(snapshot.scenario, customers, datasets)
    if len({b.customer_ref for b in snapshot.bindings}) != len(snapshot.bindings):
        raise ValueError("duplicate customer serving binding")
    if {b.customer_ref for b in snapshot.bindings} != set(customers):
        raise ValueError("each participant requires exactly one serving binding")
    if {b.serving_profile_ref for b in snapshot.bindings} != set(profiles):
        raise ValueError("missing or unused serving-profile reference in snapshot")
    if {b.worker_group_ref for b in snapshot.bindings} != set(groups):
        raise ValueError("missing or unused worker-group reference in snapshot")
    deployments = {}
    tenants_per_group = defaultdict(set)
    reservations = defaultdict(int)
    for binding in snapshot.bindings:
        customer = customers[binding.customer_ref]
        source_ref = snapshot.derivation.get(customer.ref, customer.ref)
        for manifest in snapshot.preparation:
            if (
                manifest.customer_ref == source_ref
                and manifest.chat_template_kwargs
                != profiles[binding.serving_profile_ref].engine.chat_template_kwargs
            ):
                raise ValueError(
                    f"{manifest.id}: prepared data uses different chat-template settings"
                )
        profile = profiles[binding.serving_profile_ref]
        group = groups[binding.worker_group_ref]
        if customer.model_alias != profile.model.alias:
            raise ValueError(f"{customer.id}: model alias differs from serving profile")
        if customer.limits.max_rendered_input_tokens + customer.generation.max_output_tokens > (
            profile.engine.max_model_len
        ):
            raise ValueError(f"{customer.id}: input plus output exceeds serving context")
        if profile.worker_class != group.worker_class:
            raise ValueError("serving placement differs from worker class")
        if profile.hardware.vendor != group.hardware.vendor:
            raise ValueError("serving GPU vendor differs from worker hardware")
        if profile.hardware.gpu_model and profile.hardware.gpu_model != group.hardware.gpu_model:
            raise ValueError("serving GPU model differs from worker hardware")
        if profile.hardware.min_vram_gib_per_gpu > group.hardware.vram_gib_per_gpu:
            raise ValueError("worker GPU memory is below serving requirement")
        if profile.topology.gpus_per_replica > group.hardware.gpu_count:
            raise ValueError("a replica must fit within one worker instance")
        signature = (profile.ref, group.ref)
        if binding.deployment_id in deployments and deployments[binding.deployment_id] != signature:
            raise ValueError("deployment identity is reused for incompatible serving assignments")
        deployments[binding.deployment_id] = signature
        tenants_per_group[group.ref].add(customer.ref)
        reservations[binding.deployment_id] += customer.reserved_concurrency
    allocated = defaultdict(int)
    for deployment_id, (profile_ref, group_ref) in deployments.items():
        profile = profiles[profile_ref]
        allocated[group_ref] += profile.topology.replicas * profile.topology.gpus_per_replica
        if reservations[deployment_id] > profile.engine.max_num_seqs * profile.topology.replicas:
            raise ValueError("provisioned reservations exceed serving concurrency")
    for binding in snapshot.bindings:
        if customers[binding.customer_ref].tier == "dedicated":
            if len(tenants_per_group[binding.worker_group_ref]) != 1:
                raise ValueError("dedicated customer must have an exclusive worker group")
    for group_ref, gpu_count in allocated.items():
        group = groups[group_ref]
        if gpu_count > group.desired_instances * group.hardware.gpu_count:
            raise ValueError("aggregate serving GPU allocation exceeds worker-group capacity")


def validate_run_result(snapshot: RunSnapshot, result: RunResult) -> None:
    customers = {customer.ref: customer for customer in snapshot.customers}
    if set(result.customer_requests) != set(customers):
        raise ValueError("per-customer request counts must cover exactly the run participants")
    if result.customer_sessions and set(result.customer_sessions) != set(customers):
        raise ValueError("session accounting must cover exactly the run participants")
    expected = {(c.ref, target.metric) for c in snapshot.customers for target in c.slo.targets}
    actual = {(a.customer_ref, a.metric) for a in result.slo_assessments}
    if result.validity == "valid" and actual != expected:
        raise ValueError("valid result requires assessments for every customer SLO target")
    for assessment in result.slo_assessments:
        customer = customers.get(assessment.customer_ref)
        if customer is None:
            raise ValueError("SLO assessment references a customer outside the run")
        targets = {target.metric: target for target in customer.slo.targets}
        target = targets.get(assessment.metric)
        if target is None or (
            assessment.threshold_ms != target.threshold_ms
            or assessment.minimum_attainment_fraction != target.minimum_attainment_fraction
            or assessment.minimum_eligible_requests != customer.slo.minimum_eligible_requests
        ):
            raise ValueError("SLO assessment changes the frozen customer target")
        for window in assessment.windows:
            end = window.offset_s + window.duration_s
            if end > snapshot.scenario.measurement_duration_s:
                raise ValueError("SLO window extends beyond scenario measurement duration")
            if window.offset_s % customer.slo.window_s or window.duration_s != min(
                customer.slo.window_s,
                snapshot.scenario.measurement_duration_s - window.offset_s,
            ):
                raise ValueError("SLO window differs from the customer window policy")
        expected_offsets = set(
            range(0, snapshot.scenario.measurement_duration_s, customer.slo.window_s)
        )
        if {window.offset_s for window in assessment.windows} != expected_offsets:
            raise ValueError("SLO assessment omits a measurement window")
        if assessment.metric == "session_latency_ms":
            counts = result.customer_sessions.get(customer.ref)
            if counts is None:
                raise ValueError("session SLO requires session accounting")
        else:
            counts = result.customer_requests[customer.ref]
        if (
            sum(window.eligible_requests for window in assessment.windows)
            != counts.eligible_for_slo
        ):
            raise ValueError("SLO windows must account for all eligible customer requests")
        if sum(window.good_requests for window in assessment.windows) > counts.completed:
            raise ValueError("good SLO requests cannot exceed completed customer requests")
    for record in (*result.measurements, *result.quality):
        if record.customer_ref not in customers:
            raise ValueError("measurement or quality record references a customer outside the run")
    for measurement in result.measurements:
        customer = customers[measurement.customer_ref]
        if (
            not customer.generation.streaming
            and {"ttft_ms", "tpot_ms"} & measurement.latencies.keys()
        ):
            raise ValueError("non-streaming customer cannot report streaming latency metrics")
    for quality in result.quality:
        if quality.evaluator != customers[quality.customer_ref].quality.evaluator:
            raise ValueError("quality evaluator differs from customer configuration")
        if quality.evaluator == "not_evaluated" and quality.status != "not_evaluated":
            raise ValueError("replay cannot claim a functional agent quality score")


def validate_for_execution(snapshot: RunSnapshot) -> None:
    """Offline completeness preflight, not a capacity, compatibility or readiness guarantee."""
    from .runs import RunSnapshot

    snapshot = RunSnapshot.model_validate_json(snapshot.model_dump_json())
    errors = []
    for profile in snapshot.serving_profiles:
        if profile.draft or not is_image_pin(profile.engine.image):
            errors.append(f"{profile.ref}: draft serving profile or unresolved image pin")
        if not is_commit_pin(profile.model.revision) or not is_commit_pin(
            profile.model.tokenizer_revision
        ):
            errors.append(f"{profile.ref}: unresolved model/tokenizer pin")
    for group in snapshot.worker_groups:
        if group.instance_type is None or group.region is None:
            errors.append(f"{group.ref}: unresolved Lambda instance type/region")
        minimum_s = (
            snapshot.scenario.warmup.duration_s
            + snapshot.scenario.measurement_duration_s
            + snapshot.scenario.drain_timeout_s
        )
        if (
            group.ttl_minutes is not None
            and group.ttl_minutes * 60 < minimum_s + group.drain_timeout_s
        ):
            errors.append(f"{group.ref}: lease is shorter than run plus cleanup")
    for dataset in snapshot.datasets:
        if dataset.prepared is None:
            errors.append(f"{dataset.ref}: missing prepared dataset")
        if dataset.source.revision is None or dataset.source.revision in {
            "main",
            "master",
            "latest",
        }:
            errors.append(f"{dataset.ref}: unresolved dataset source revision")
        if dataset.source.license is None:
            errors.append(f"{dataset.ref}: dataset license is unreviewed")
        if dataset.prepared and dataset.prepared.uri.startswith("fixture://"):
            errors.append(f"{dataset.ref}: fixture artifact is not benchmark data")
    profiles = {profile.ref: profile for profile in snapshot.serving_profiles}
    customers = {customer.ref: customer for customer in snapshot.customers}
    datasets = {dataset.ref: dataset for dataset in snapshot.datasets}
    for binding in snapshot.bindings:
        customer = customers[binding.customer_ref]
        refs = [customer.workload.dataset_ref]
        source_ref = snapshot.derivation.get(customer.ref, customer.ref)
        for manifest in snapshot.preparation:
            if (
                manifest.customer_ref == source_ref
                and manifest.tokenizer_id != profiles[binding.serving_profile_ref].model.repository
            ):
                errors.append(f"{manifest.id}: tokenizer repository differs from serving model")
        if warmup_ref := snapshot.scenario.warmup.datasets.get(customer.id):
            refs.append(warmup_ref)
        for ref in refs:
            prepared = datasets[ref].prepared
            if prepared:
                if (
                    prepared.tokenizer_revision
                    != profiles[binding.serving_profile_ref].model.tokenizer_revision
                ):
                    errors.append(f"{ref}: prepared data uses a different tokenizer")
                if prepared.max_rendered_input_tokens > customer.limits.max_rendered_input_tokens:
                    errors.append(f"{ref}: prepared input exceeds customer input limit")
                if prepared.prompt_template_sha256 != customer.workload.prompt_template_digest:
                    errors.append(f"{ref}: prepared data uses a different prompt template")
    versions = snapshot.versions
    if not is_image_pin(versions.runner_image):
        errors.append("unresolved runner image pin")
    version_values = {
        "aiperf": versions.aiperf_version,
        "evaluator": versions.evaluator_version,
        **{
            key: versions.platform_versions.get(key)
            for key in ("kubernetes", "cilium", "vllm")
            + (
                ("serving-chart",)
                if snapshot.target and snapshot.target.serving_stack == "direct-vllm"
                else ("model-pool-chart", "llm-d")
            )
        },
    }
    for name, version in version_values.items():
        if not version or not re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][\w.-]+)?", version):
            errors.append(f"unresolved {name} version")
    if snapshot.inventory is None or snapshot.inventory.fixture:
        errors.append("missing non-fixture inventory snapshot")
    if snapshot.admission_schedule is None or snapshot.admission_schedule.sha256 is None:
        errors.append("missing checksummed admission schedule")
    elif snapshot.admission_schedule.uri.startswith("fixture://"):
        errors.append("fixture admission schedule is not executable")
    if errors:
        raise ValueError("Execution preflight failed:\n- " + "\n- ".join(errors))
