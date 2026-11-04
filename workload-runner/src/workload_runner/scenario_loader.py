"""Compile authored scenarios into independent, fully materialized execution snapshots."""

import json
import random
from pathlib import Path

from platform_contracts import (
    Admission,
    ExecutionTarget,
    PreparationManifest,
    PreparedEntry,
    RunSnapshot,
    ServingBinding,
    build_run_snapshot,
)
from platform_contracts.datasets import DatasetManifest, PreparedDataset

from .artifacts import artifact_path, atomic_json, canonical, digest, read_jsonl, write_jsonl


def default_bindings(catalog, scenario_ref):
    deployments = {
        "gec-baseline": "gec",
        "code-agent-baseline": "code-large",
        "shared-small-baseline": "shared-small",
        "shared-medium-baseline": "shared-medium",
    }
    result = []
    for ref in catalog.get("scenario", scenario_ref).participants:
        customer = catalog.get("customer", ref)
        profiles = [
            p
            for p in catalog.records["serving-profile"].values()
            if p.model.alias == customer.model_alias
        ]
        if len(profiles) != 1:
            raise ValueError(f"{ref}: ambiguous serving profiles; supply explicit bindings")
        profile = profiles[0]
        groups = [
            g
            for g in catalog.records["worker-group"].values()
            if g.worker_class == profile.worker_class
        ]
        if len(groups) != 1:
            raise ValueError(f"{ref}: ambiguous worker groups; supply explicit bindings")
        group = groups[0]
        result.append(
            ServingBinding(
                customer_ref=ref,
                serving_profile_ref=profile.ref,
                worker_group_ref=group.ref,
                deployment_id=deployments.get(profile.id, profile.id),
            )
        )
    return tuple(result)


def random_stream(seed, identity):
    return random.Random(int(digest(f"{seed}:{identity}".encode()), 16))


def admission_times(rate_per_second, duration_s, rng, pattern):
    elapsed = 0.0 if pattern == "constant" else rng.expovariate(rate_per_second)
    while elapsed < duration_s:
        yield elapsed * 1000
        elapsed += (
            1 / rate_per_second if pattern == "constant" else rng.expovariate(rate_per_second)
        )


def compile_run(
    catalog,
    scenario_ref: str,
    preparations: dict[str, PreparationManifest],
    target: ExecutionTarget,
    output: Path,
    *,
    smoke=False,
    bindings=None,
    inventory=None,
    versions=None,
    sustained=False,
    duration_variant=None,
):
    if smoke and sustained:
        raise ValueError("smoke and sustained variants are mutually exclusive")
    if target.mode == "benchmark" and bindings is None:
        raise ValueError("benchmark planning requires explicit pinned serving bindings")
    base = build_run_snapshot(
        catalog,
        scenario_ref,
        bindings if bindings is not None else default_bindings(catalog, scenario_ref),
        inventory=inventory,
        versions=versions,
    )
    data = base.model_dump(mode="json")
    data["target"] = target.model_dump(mode="json")
    data["versions"]["aiperf_version"] = "0.13.0"
    data["versions"]["evaluator_version"] = "0.1.0"
    identity = digest(
        canonical(
            {
                "base": base,
                "preparations": preparations,
                "target": target,
                "smoke": smoke,
                "sustained": sustained,
                **({"duration_variant": duration_variant} if duration_variant else {}),
            }
        )
    )[:12]
    data["scenario"]["id"] += (
        "-smoke-" if smoke else "-sustained-" if sustained else "-prepared-"
    ) + identity
    data["scenario"]["revision"] = 1
    data["scenario"]["notes"] = f"Derived from {base.scenario.ref}; immutable inputs {identity}."
    if sustained:
        if [(p["id"], p["duration_s"]) for p in data["scenario"]["phases"]] != [
            ("steady", 180),
            ("burst", 60),
            ("recovery", 360),
        ]:
            raise ValueError(
                "sustained variant requires the canonical steady/burst/recovery phases"
            )
        data["scenario"]["phases"][2]["duration_s"] = 960
    if smoke:
        if target.mode != "local-stub":
            raise ValueError(
                "smoke overrides are restricted to explicitly labelled local-stub runs"
            )
        data["scenario"]["warmup"]["duration_s"] = 1
        data["scenario"]["drain_timeout_s"] = 1
        for phase in data["scenario"]["phases"]:
            phase["duration_s"] = 1
            for arrival in phase["arrivals"].values():
                arrival.update(
                    rate=12.0 if phase["id"] == "burst" else 4.0, per="second", pattern="constant"
                )
    id_map = {}
    ref_map = {}
    datasets = []
    prepared_entries = {}
    for customer in data["customers"]:
        source_customer_ref = f"{customer['id']}@{customer['revision']}"
        manifest = preparations.get(source_customer_ref)
        if manifest is None or manifest.customer_ref != source_customer_ref:
            raise ValueError(f"missing preparation for {source_customer_ref}")
        template_digest = catalog.get(
            "customer", source_customer_ref
        ).workload.prompt_template_digest
        if manifest.prompt_template_sha256 != template_digest:
            raise ValueError("prepared inputs use a different customer prompt template")
        path = artifact_path(manifest.artifact.uri, manifest.artifact.sha256)
        entries = list(read_jsonl(path, PreparedEntry))
        if (
            len(entries) != manifest.entry_count
            or sum(e.warmup for e in entries) != manifest.warmup_count
        ):
            raise ValueError("prepared entries disagree with manifest")
        old_id = customer["id"]
        customer["id"] += ("-smoke-" if smoke else "-prepared-") + identity
        customer["revision"] = 1
        customer["notes"] = f"Derived from {source_customer_ref}; source data {manifest.id}."
        id_map[old_id] = customer["id"]
        ref_map[source_customer_ref] = customer["id"] + "@1"
        customer["workload"]["dataset_ref"] = manifest.id + "@1"
        if smoke:
            customer["limits"]["request_timeout_ms"] = 500
            customer["traffic_allowance"].update(
                sustained_per_second=4.0, burst_per_second=12.0, max_burst_duration_s=1
            )
            customer["slo"].update(window_s=1, minimum_eligible_requests=1)
            for slo in customer["slo"]["targets"]:
                slo["threshold_ms"] = 100.0
        source = catalog.get("dataset-manifest", manifest.source_manifest_refs[0])
        dataset = source.model_dump(mode="json")
        dataset.update(
            id=manifest.id,
            revision=1,
            notes=f"Resolved preparation {manifest.id}; sources {manifest.source_manifest_refs}.",
        )
        dataset["source"]["revision"] = next(iter(manifest.source_revisions.values()), None)
        dataset["source"]["license"] = next(iter(manifest.licenses.values()), None)
        if manifest.data_kind == "synthetic":
            dataset["source"].update(
                url=next(iter(manifest.source_urls.values())),
                subset="deterministic synthetic fixtures; no public corpus content",
                filters={},
            )
            dataset["limitations"] = ["Generated test data; emulated tokenization and output."]
        dataset["prepared"] = PreparedDataset(
            uri=manifest.artifact.uri,
            sha256=manifest.artifact.sha256,
            record_count=manifest.entry_count,
            tokenizer_revision=manifest.tokenizer_revision,
            prompt_template_sha256=manifest.prompt_template_sha256,
            max_rendered_input_tokens=max(1, manifest.max_rendered_input_tokens),
        ).model_dump(mode="json")
        datasets.append(DatasetManifest.model_validate(dataset))
        prepared_entries[ref_map[source_customer_ref]] = entries
    scenario = data["scenario"]
    scenario["participants"] = [ref_map[ref] for ref in scenario["participants"]]
    scenario["warmup"]["datasets"] = {
        id_map[c.id]: preparations[c.ref].id + "@1" for c in base.customers
    }
    for phase in scenario["phases"]:
        phase["arrivals"] = {id_map[key]: value for key, value in phase["arrivals"].items()}
    for binding in data["bindings"]:
        binding["customer_ref"] = ref_map[binding["customer_ref"]]
    data["datasets"] = [dataset.model_dump(mode="json") for dataset in datasets]
    data["preparation"] = [preparations[c.ref].model_dump(mode="json") for c in base.customers]
    data["derivation"] = {
        **{value: key for key, value in ref_map.items()},
        "source_scenario_ref": base.scenario.ref,
        "source_snapshot_sha256": digest(canonical(base)),
        "duration_variant": duration_variant
        or ("smoke" if smoke else "sustained-20m" if sustained else "canonical"),
    }
    data["target"]["credential_env"] = {
        id_map.get(key, key): value for key, value in data["target"]["credential_env"].items()
    }
    snapshot = RunSnapshot.model_validate(data)
    profiles = {p.ref: p for p in snapshot.serving_profiles}
    for binding in snapshot.bindings:
        source_ref = snapshot.derivation[binding.customer_ref]
        if (
            preparations[source_ref].chat_template_kwargs
            != profiles[binding.serving_profile_ref].engine.chat_template_kwargs
        ):
            raise ValueError("prepared and serving chat-template settings differ")
    admissions = []
    for customer in snapshot.customers:
        entries = prepared_entries[customer.ref]
        source_ref = snapshot.derivation[customer.ref]
        pools = {
            warm: sorted([e for e in entries if e.warmup == warm], key=lambda e: e.id)
            for warm in (True, False)
        }
        for warm, pool in pools.items():
            random_stream(
                snapshot.scenario.seed, f"{base.scenario.ref}:{source_ref}:samples:{warm}"
            ).shuffle(pool)
        phases = [
            (
                "warmup",
                snapshot.scenario.warmup.duration_s,
                snapshot.scenario.phases[0].arrivals[customer.id],
                True,
            )
        ]
        phases += [
            (p.id, p.duration_s, p.arrivals[customer.id], False) for p in snapshot.scenario.phases
        ]
        offset = 0.0
        used = {True: 0, False: 0}
        for name, duration, arrival, warm in phases:
            rng = random_stream(snapshot.scenario.seed, f"{base.scenario.ref}:{source_ref}:{name}")
            for number, when in enumerate(
                admission_times(arrival.per_second, duration, rng, arrival.pattern)
            ):
                if used[warm] >= len(pools[warm]):
                    raise ValueError(
                        f"{customer.ref}: dataset exhaustion in {name}; "
                        "prepare more inputs or derive a smaller scenario"
                    )
                entry = pools[warm][used[warm]]
                used[warm] += 1
                key = digest(f"{identity}:{customer.ref}:{name}:{number}".encode())[:20]
                admissions.append(
                    Admission(
                        id="admission-" + key,
                        customer_ref=customer.ref,
                        entry_id=entry.id,
                        scheduled_offset_ms=offset + when,
                        warmup=warm,
                        phase=name,
                    )
                )
            offset += duration * 1000
    admissions.sort(key=lambda admission: (admission.scheduled_offset_ms, admission.id))
    if not admissions:
        raise ValueError("scenario produced no admissions")
    output.mkdir(parents=True, exist_ok=True)
    checksum = digest(b"".join(canonical(admission) + b"\n" for admission in admissions))
    data = snapshot.model_dump(mode="json")
    data["admission_schedule"] = {
        "uri": (output / "admission-schedule.jsonl").resolve().as_uri(),
        "sha256": checksum,
    }
    snapshot = RunSnapshot.model_validate(data)
    if (output / "snapshot.json").exists():
        if json.loads((output / "snapshot.json").read_text()) != snapshot.model_dump(mode="json"):
            raise ValueError("plan directory already contains different immutable inputs")
    write_jsonl(output / "admission-schedule.jsonl", admissions)
    atomic_json(output / "snapshot.json", snapshot)
    atomic_json(
        output / "management-bundle.json",
        {
            "source": "stream-a-preparation",
            "provenance": "synthetic" if target.mode == "local-stub" else "unverified",
            "document": {
                "id": "prepared-run-" + digest(canonical(snapshot))[:24],
                "fixture": target.mode == "local-stub",
                "snapshot": snapshot,
            },
        },
    )
    return snapshot
