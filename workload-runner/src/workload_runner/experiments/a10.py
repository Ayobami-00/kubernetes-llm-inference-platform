"""The fixed retained-A10 comparison; no provider or Kubernetes dependencies."""

import copy
from difflib import SequenceMatcher

from platform_contracts import ScenarioSpec, ServingProfile

from workload_runner.artifacts import canonical, digest

INSTANCE = "8f2e29b7279b42148ffaaed4a4dcdd8e"
NODE = "writer-a10-retained"
NODE_UID = "9b5d6b5c-d906-47eb-b038-47e89f237641"
BOOT_ID = "18711776-5b58-489c-90bf-c593d29ed277"
DEPLOYMENT = "writer-assist-a10"
GROUP = "writer-a10-retained-capacity"
ENDPOINT = "http://inference.llm-d.svc.cluster.local:8080/v1/chat/completions"
VARIANTS = ("baseline", "ngram")
SPECULATION = {
    "method": "ngram",
    "num_speculative_tokens": 5,
    "prompt_lookup_min": 2,
    "prompt_lookup_max": 5,
}


def comparison_scenario(canonical_scenario):
    """Distinct authored identity; canonical scenarios and targets stay unchanged."""
    value = copy.deepcopy(canonical_scenario.model_dump(mode="json"))
    if value["participants"] != ["writer-assist@1"] or [
        (p["id"], p["duration_s"]) for p in value["phases"]
    ] != [("steady", 180), ("burst", 60), ("recovery", 360)]:
        raise ValueError("A10 comparison requires the canonical WriterAssist source")
    value.update(
        id="writer-assist-a10-paired-5m",
        revision=1,
        seed=42,
        faults=[],
        drain_timeout_s=30,
        notes=(
            f"Derived from {canonical_scenario.ref}: 300 scored seconds for the paired "
            "A10 experiment; unchanged customer rates, limits and targets."
        ),
    )
    value["phases"][2]["duration_s"] = 60
    value["warmup"]["duration_s"] = 60
    return ScenarioSpec.model_validate(value)


def validate_profiles(profiles):
    baseline, ngram = (ServingProfile.model_validate(profiles[v]) for v in VARIANTS)
    if baseline.engine.speculative_decoding is not None or (
        ngram.engine.speculative_decoding is None
        or ngram.engine.speculative_decoding.model_dump(mode="json") != SPECULATION
    ):
        raise ValueError("Only the reviewed baseline and fixed n-gram settings are permitted")
    values = [p.model_dump(mode="json") for p in (baseline, ngram)]
    for value in values:
        value.pop("revision")
        value.pop("notes", None)
        value["engine"].pop("speculative_decoding")
    if values[0] != values[1]:
        raise ValueError("Serving controls differ beyond speculative decoding")
    if (
        baseline.model.repository != "Qwen/Qwen3-4B"
        or baseline.hardware.gpu_model != "A10"
        or baseline.engine.prefix_caching
        or baseline.engine.chat_template_kwargs != {"enable_thinking": False}
        or baseline.topology.gpus_per_replica != 1
        or baseline.topology.replicas != 1
    ):
        raise ValueError("Profile is outside the retained A10 experiment scope")
    return {v: p for v, p in zip(VARIANTS, (baseline, ngram), strict=True)}


def paired_schedule(rows):
    """Plan-specific IDs differ; every material experimental admission must match."""
    return [{k: v for k, v in row.items() if k not in {"id", "customer_ref"}} for row in rows]


def schedule_identity(rows):
    return digest(canonical(paired_schedule(rows)))


def token_edit_distance(source, output):
    previous = list(range(len(output) + 1))
    for i, left in enumerate(source, start=1):
        current = [i]
        for j, right in enumerate(output, start=1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = current
    return previous[-1]


def token_overlap(source, output):
    """Ordered, disjoint exact token blocks; no bag-of-words double counting."""
    blocks = SequenceMatcher(a=source, b=output, autojunk=False).get_matching_blocks()
    copied = sum(block.size for block in blocks)
    distance = token_edit_distance(source, output)
    return {
        "source_tokens": len(source),
        "output_tokens": len(output),
        "matched_tokens": copied,
        "output_copy_fraction": copied / len(output) if output else None,
        "longest_matching_span": max((block.size for block in blocks), default=0),
        "token_edit_distance": distance,
        "normalized_token_edit_distance": distance / max(len(source), len(output), 1),
    }
