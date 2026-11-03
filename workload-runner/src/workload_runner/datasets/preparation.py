"""Normalize bounded corpora into immutable, checksummed run inputs."""

import json
import re
from collections import Counter
from pathlib import Path

from platform_contracts import PreparationManifest, PreparedEntry, PreparedTurn
from platform_contracts.common import Artifact

from workload_runner.artifacts import atomic_json, canonical, digest, write_jsonl

TRANSFORM_VERSION = "0.4.1"


class TokenCounter:
    def __init__(self, repository=None, revision=None):
        self.tokenizer = None
        self.id = "fixture-regex-v1"
        self.revision = "fixture-regex-v1"
        if repository:
            from huggingface_hub import HfApi
            from transformers import AutoTokenizer

            self.revision = revision or HfApi().model_info(repository).sha
            self.id = repository
            self.tokenizer = AutoTokenizer.from_pretrained(
                repository, revision=self.revision, trust_remote_code=False
            )

    def count(self, messages, tools=()) -> int:
        if self.tokenizer:
            kwargs = {"tools": list(tools)} if tools else {}
            return len(
                self.tokenizer.apply_chat_template(
                    list(messages),
                    tokenize=True,
                    return_dict=False,
                    add_generation_prompt=True,
                    **kwargs,
                )
            )
        return len(re.findall(r"\w+|[^\w\s]", json.dumps([messages, tools], ensure_ascii=False)))


def messages_for(customer, **values):
    template = customer.workload.prompt_template
    if template is None:
        raise ValueError("recorded contexts must provide their own messages")
    return [
        {"role": "system", "content": template.system},
        {"role": "user", "content": template.user.format(**values)},
    ]


def synthetic_entries(customer, count):
    """Generated examples have their own IDs and never borrow public source identities."""
    for i in range(count):
        task = customer.workload.task
        references = {}
        mode = "recorded"
        if task == "sentence_correction":
            text = f"She go to school on day {i}."
            turns = [
                (
                    messages_for(customer, text=text),
                    {"corrections": [f"She goes to school on day {i}."]},
                    (),
                    {},
                )
            ]
        elif task == "coding":
            tool = {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            first = [
                {"role": "system", "content": "Inspect the code. This is a synthetic replay."},
                {"role": "user", "content": f"Inspect example {i}."},
            ]
            second = first + [
                {"role": "assistant", "content": "Recorded inspection."},
                {"role": "user", "content": "Suggest a fix."},
            ]
            turns = [
                (first, references, (tool,), {"max_tokens": 4096}),
                (second, references, (tool,), {"max_tokens": 4096}),
            ]
        elif task == "tutoring":
            mode = "live"
            turns = [
                (
                    messages_for(customer, dialogue=f"Student: How do I solve {i} + 2?"),
                    {"teacher": "What operation would help?"},
                    (),
                    {},
                ),
                (
                    [{"role": "user", "content": "Can you give me a hint?"}],
                    {"teacher": "Try counting two more."},
                    (),
                    {},
                ),
            ]
        elif task == "extraction":
            clause = "Either party may terminate with thirty days notice."
            turns = [
                (
                    messages_for(
                        customer, context=f"Contract {i}. {clause}", clause_type="termination"
                    ),
                    {"answers": [clause]},
                    (),
                    {},
                )
            ]
        else:
            mode = "live"
            turns = [
                (
                    messages_for(
                        customer,
                        question=f"What is the capital of France? Case {i}",
                        context="France: Paris is the capital of France.",
                    ),
                    {},
                    (),
                    {},
                ),
                (
                    [{"role": "user", "content": "Return JSON with answer and supporting_facts."}],
                    {"answer": "Paris", "supporting_facts": [["France", 0]]},
                    (),
                    {},
                ),
            ]
        yield {
            "source_id": f"synthetic:{customer.id}:{i}",
            "turns": turns,
            "context_mode": mode,
            "adaptations": ["synthetic_example"],
        }


def prepare_customer(
    catalog,
    customer_ref: str,
    output: Path,
    *,
    source="synthetic",
    limit=None,
    byte_limit=1024**3,
    tokenizer=None,
    max_turns=None,
):
    customer = catalog.get("customer", customer_ref)
    limit = (
        (200 if customer.workload.interaction == "session" else 1000) if limit is None else limit
    )
    if limit < 2:
        raise ValueError("preparation requires at least two examples for disjoint cohorts")
    turn_bound = (
        max_turns if max_turns is not None else (8 if customer.workload.task == "coding" else None)
    )
    if turn_bound is not None and turn_bound < 1:
        raise ValueError("max_turns must be positive")
    if isinstance(tokenizer, str):
        tokenizer = TokenCounter(tokenizer)
    if tokenizer is None and source == "public":
        profile = next(
            p
            for p in catalog.records["serving-profile"].values()
            if p.model.alias == customer.model_alias
        )
        tokenizer = TokenCounter(profile.model.repository, profile.model.tokenizer_revision)
    counter = tokenizer or TokenCounter()
    source_ref = customer.workload.dataset_ref
    source_manifest = catalog.get("dataset-manifest", source_ref)
    metadata = {"revisions": {}, "urls": {}, "licenses": {}, "bytes": 0}
    if source == "public":
        from .public import public_entries

        rows = public_entries(
            customer, source_manifest, limit, output / "raw", byte_limit, metadata
        )
    else:
        rows = synthetic_entries(customer, limit)
        metadata.update(
            revisions={"generator": TRANSFORM_VERSION},
            urls={"generator": "https://github.com/Ayobami-00/kubernetes-llm-inference-platform"},
            licenses={"generator": None},
        )
    entries = []
    exclusions = Counter()
    adaptations = Counter()
    scanned = 0
    max_tokens = 0
    for row in rows:
        scanned += 1
        turns = []
        live_prefix = []
        entry_changes = list(row.get("adaptations", ()))
        if turn_bound and len(row["turns"]) > turn_bound:
            entry_changes.append("contiguous_turn_bound_prefix")
            exclusions["turn_bound_excluded_calls"] += len(row["turns"]) - turn_bound
        for index, (messages, refs, tools, controls) in enumerate(row["turns"]):
            if turn_bound and index >= turn_bound:
                break
            mode = row.get("context_mode", "recorded")
            live_prefix.extend(messages)
            rendered = live_prefix if mode == "live" else messages
            # A resource bound is an explicit exclusion, never a context truncation
            # or a claim about token length. Pathological recorded text can make
            # BPE preparation unbounded long before a full token count is available.
            if (
                len(json.dumps([rendered, tools], ensure_ascii=False))
                > customer.limits.max_rendered_input_tokens * 16
            ):
                exclusions["preparation_character_bound_turns"] += len(row["turns"]) - index
                if turns:
                    entry_changes.append("contiguous_context_prefix")
                break
            count = counter.count(rendered, tools)
            bound = count + (
                index * (customer.generation.max_output_tokens + 16) if mode == "live" else 0
            )
            if bound > customer.limits.max_rendered_input_tokens:
                exclusions["context_limit_turns"] += len(row["turns"]) - index
                if turns:
                    entry_changes.append("contiguous_context_prefix")
                break
            effective = dict(controls)
            turn_changes = list(row.get("call_adaptations", [()] * len(row["turns"]))[index])
            maximum = effective.pop(
                "max_completion_tokens",
                effective.get("max_tokens", customer.generation.max_output_tokens),
            )
            if maximum > customer.generation.max_output_tokens:
                turn_changes.append("output_cap")
            effective["max_tokens"] = min(maximum, customer.generation.max_output_tokens)
            effective.setdefault("temperature", customer.generation.temperature)
            turns.append(
                PreparedTurn(
                    messages=messages,
                    tools=tools,
                    source_tools=row.get("source_tools", [t[2] for t in row["turns"]])[index],
                    references=refs,
                    source_controls=row.get("source_controls", [t[3] for t in row["turns"]])[index],
                    controls=effective,
                    input_tokens=count,
                    delay_ms=float(row.get("delays", [0] * len(row["turns"]))[index]),
                    adaptations=turn_changes,
                )
            )
            adaptations.update(turn_changes)
            max_tokens = max(max_tokens, bound)
        if not turns:
            exclusions["no_compatible_turns"] += 1
            continue
        identity = "sample-" + digest(canonical({"source": row["source_id"], "turns": turns}))[:20]
        entries.append(
            PreparedEntry(
                id=identity,
                source_id=str(row["source_id"]),
                warmup=bool(row.get("warmup", False)),
                turns=turns,
                context_mode=row.get("context_mode", "recorded"),
                adaptations=entry_changes,
            )
        )
        adaptations.update(entry_changes)
        if len(entries) >= limit:
            break
    if len(entries) < 2:
        raise ValueError(
            f"{customer.id}: insufficient compatible data; scanned={scanned}, "
            f"selected={len(entries)}, exclusions={dict(exclusions)}"
        )
    if len(entries) < limit:
        exclusions["source_exhausted_before_requested_bound"] = limit - len(entries)
    if not any(entry.warmup for entry in entries):
        # Stable split by source identity; no source example crosses cohorts.
        entries.sort(key=lambda entry: digest(entry.source_id.encode()))
        warmup_count = max(1, len(entries) // 5)
        entries = [
            entry.model_copy(update={"warmup": index < warmup_count})
            for index, entry in enumerate(entries)
        ]
    unique_ids = [entry.source_id for entry in entries]
    if len(unique_ids) != len(set(unique_ids)):
        raise ValueError("source returned duplicate identities")
    identity = digest(
        canonical(
            {
                "customer": customer,
                "source": source,
                "entries": entries,
                "versions": metadata["revisions"],
                "licenses": metadata["licenses"],
                "license_evidence": metadata.get("license_evidence", {}),
                "tokenizer": [counter.id, counter.revision],
                "transform": TRANSFORM_VERSION,
            }
        )
    )
    directory = output / source / customer.id / identity
    artifact = directory / "entries.jsonl"
    checksum = write_jsonl(artifact, entries)
    manifest = PreparationManifest(
        id=f"prepared-{customer.id}-{identity[:12]}",
        customer_ref=customer.ref,
        source_manifest_refs=[source_ref]
        + (["jfleg-dev@1"] if customer.id == "writer-assist" else []),
        data_kind=source,
        source_revisions=metadata["revisions"],
        source_urls=metadata["urls"],
        licenses=metadata["licenses"],
        transform_version=TRANSFORM_VERSION,
        tokenizer_id=counter.id,
        tokenizer_revision=counter.revision,
        token_accounting="model_tokenizer" if counter.tokenizer else "emulated",
        prompt_template_sha256=customer.workload.prompt_template_digest,
        artifact=Artifact(uri=artifact.resolve().as_uri(), sha256=checksum),
        entry_count=len(entries),
        warmup_count=sum(entry.warmup for entry in entries),
        max_rendered_input_tokens=max_tokens,
        scanned=scanned,
        downloaded_bytes=metadata["bytes"],
        exclusions=dict(exclusions),
        adaptations=dict(adaptations),
        license_evidence=metadata.get("license_evidence", {}),
        selection={
            "requested_bound": limit,
            "download_limit_bytes": byte_limit,
            "cohort_policy": "source split for JFLEG; otherwise source-ID hash 20% warmup",
            "max_characters_before_tokenization": customer.limits.max_rendered_input_tokens * 16,
            "maximum_contiguous_turns": turn_bound,
        },
    )
    atomic_json(directory / "manifest.json", manifest)
    atomic_json(output / source / customer.id / "latest.json", manifest)
    return manifest
