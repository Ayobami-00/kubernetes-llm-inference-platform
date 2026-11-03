"""Reference-based task metrics. Task proxies never imply production or agent success."""

import json
import math
import random
import re
import string
from collections import Counter

from platform_contracts import PreparedEntry
from platform_contracts.runs import QualityResult
from workload_runner.artifacts import artifact_path, atomic_json, read_jsonl


def normalized(text):
    text = "".join(c for c in text.lower() if c not in string.punctuation)
    return " ".join(re.sub(r"\b(a|an|the)\b", " ", text).split())


def answer_scores(predicted, reference):
    p, r = normalized(predicted), normalized(reference)
    overlap = sum((Counter(p.split()) & Counter(r.split())).values())
    f1 = 2 * overlap / (len(p.split()) + len(r.split())) if p and r else float(p == r)
    if p in {"yes", "no", "noanswer"} or r in {"yes", "no", "noanswer"}:
        f1 = float(p == r)
    return float(p == r), f1


def supporting_scores(predicted, reference):
    p = {tuple(item) for item in predicted}
    r = {tuple(item) for item in reference}
    precision = len(p & r) / len(p) if p else 0.0
    recall = len(p & r) / len(r) if r else 0.0
    return float(p == r), (
        2 * precision * recall / (precision + recall) if precision + recall else 0.0
    )


def gleu_stats(source, hypothesis, reference):
    """GLEU+ sufficient statistics (Napoles et al., updated reference scorer)."""
    s, h, r = source.split(), hypothesis.split(), reference.split()
    stats = [len(h), len(r)]
    for n in range(1, 5):

        def counts(tokens, order=n):
            return Counter(tuple(tokens[i : i + order]) for i in range(len(tokens) - order + 1))

        source_grams, hyp_grams, ref_grams = counts(s), counts(h), counts(r)
        retained_errors = Counter(
            {g: count for g, count in source_grams.items() if g not in ref_grams}
        )
        matched = sum((hyp_grams & ref_grams).values())
        penalty = sum((hyp_grams & retained_errors).values())
        stats.extend((max(0, matched - penalty), max(0, len(h) - n + 1)))
    return stats


def corpus_gleu(examples, *, seed=0, iterations=500):
    """Mean corpus GLEU across uniformly sampled references; no sentence smoothing."""
    if not examples:
        return None
    stats_by_ref = [
        [gleu_stats(source, hypothesis, ref) for ref in references]
        for source, hypothesis, references in examples
    ]
    scores = []
    for iteration in range(iterations):
        # Match the upstream JFLEG reference scorer's per-iteration seed schedule.
        rng = random.Random(iteration * 101 + seed)
        total = [0] * 10
        for choices in stats_by_ref:
            stats = choices[rng.randrange(len(choices))]
            total = [a + b for a, b in zip(total, stats, strict=True)]
        if any(value == 0 for value in total):
            scores.append(0.0)
        else:
            log_precision = sum(math.log(total[i] / total[i + 1]) for i in (2, 4, 6, 8)) / 4
            scores.append(math.exp(min(0, 1 - total[1] / total[0]) + log_precision))
    return sum(scores) / len(scores)


def evaluate_quality(snapshot, records, directory, *, source_directory=None):
    source = source_directory or directory
    expected_path = source / "engine-inputs.jsonl"
    observed_path = source / "aiperf" / "transport-events.jsonl"
    expected = (
        {row["session_id"]: row for row in read_jsonl(expected_path)}
        if expected_path.exists()
        else {}
    )
    observed = (
        {
            (row["conversation_id"], row["turn_index"]): row["payload"]
            for row in read_jsonl(observed_path)
            if row["kind"] == "record"
        }
        if observed_path.exists()
        else {}
    )
    preparations = {
        p.customer_ref: {
            entry.id: entry
            for entry in read_jsonl(artifact_path(p.artifact.uri, p.artifact.sha256), PreparedEntry)
        }
        for p in snapshot.preparation
    }
    results, details = [], {}
    for customer in snapshot.customers:
        source_ref = snapshot.derivation[customer.ref]
        entries = preparations[source_ref]
        selected = [r for r in records if r.customer_ref == customer.ref and r.eligible_for_slo]
        coverage = dict(eligible=len(selected), evaluated=0, failed=0, missing=0, unscorable=0)
        gleu, metrics, bundle = [], [], []
        for record in selected:
            turn = entries[record.source_entry_id].turns[record.turn_index]
            refs = turn.references
            bundle.append(
                dict(
                    request_id=record.request_id,
                    reference=refs,
                    output=record.output_text,
                    outcome=record.outcome,
                )
            )
            if record.outcome != "completed":
                coverage["failed"] += 1
                continue
            if record.output_text is None:
                coverage["missing"] += 1
                continue
            task = customer.workload.task
            if task == "sentence_correction" and refs.get("corrections"):
                source = turn.messages[-1]["content"]
                gleu.append((source, record.output_text, refs["corrections"]))
            elif task in {"extraction", "research"} and refs:
                try:
                    value = json.loads(record.output_text)
                    if task == "extraction":
                        answer = value["clause"]
                        if not isinstance(answer, str):
                            raise ValueError("clause must be text")
                        f1 = max(answer_scores(answer, ref)[1] for ref in refs["answers"])
                        metrics.append({"adapted_extraction_f1": f1})
                    else:
                        answer = value["answer"]
                        facts = value["supporting_facts"]
                        if not isinstance(answer, str) or not isinstance(facts, list):
                            raise ValueError("invalid answer schema")
                        if any(
                            not isinstance(pair, list)
                            or len(pair) != 2
                            or not isinstance(pair[0], str)
                            or type(pair[1]) is not int
                            or pair[1] < 0
                            for pair in facts
                        ):
                            raise ValueError("invalid supporting fact pair")
                        em, f1 = answer_scores(answer, refs["answer"])
                        sem, sf1 = supporting_scores(facts, refs["supporting_facts"])
                        metrics.append(
                            dict(answer_em=em, answer_f1=f1, supporting_em=sem, supporting_f1=sf1)
                        )
                except (ValueError, TypeError, KeyError):
                    coverage["unscorable"] += 1
                    continue
            else:
                coverage["unscorable"] += 1
                continue
            coverage["evaluated"] += 1
        scores = {}
        if gleu:
            scores["gleu"] = corpus_gleu(gleu)
        if metrics:
            scores.update({k: sum(m[k] for m in metrics) / len(metrics) for k in metrics[0]})
        if customer.workload.task == "extraction" and coverage["eligible"]:
            scores["response_format_attainment"] = coverage["evaluated"] / coverage["eligible"]
        notes = (
            "Synthetic/emulated outputs; task metrics only. "
            if snapshot.target.mode == "local-stub"
            else ""
        )
        notes += (
            "Recorded inference-call replay; no tool execution or functional agent quality."
            if customer.workload.task == "coding"
            else "Review bundle only; no automated pedagogical quality judgement."
            if customer.workload.task == "tutoring"
            else "Reference-based adapted task evaluation; coverage is reported separately."
        )
        results.append(
            QualityResult(
                customer_ref=customer.ref,
                evaluator=customer.quality.evaluator,
                status="measured" if scores else "not_evaluated",
                scores=scores,
                notes=notes,
            )
        )
        details[customer.ref] = dict(
            coverage=coverage,
            scores=scores,
            notes=notes,
            review_bundle=bundle,
            preparation_adaptations=next(
                p.adaptations for p in snapshot.preparation if p.customer_ref == source_ref
            ),
            preparation_exclusions=next(
                p.exclusions for p in snapshot.preparation if p.customer_ref == source_ref
            ),
        )
        if customer.workload.task == "coding":
            detail = details[customer.ref]
            detail["replay_integrity"] = dict(
                functional_quality="not_evaluated",
                contexts="recorded; no live-response feedback",
                eligible_calls=len(selected),
                successful_calls=sum(r.outcome == "completed" for r in selected),
                selected_inputs=len({r.source_entry_id for r in selected}),
                output_cap_calls=sum(
                    "output_cap" in entries[r.source_entry_id].turns[r.turn_index].adaptations
                    for r in selected
                ),
                source_call_ids=[
                    entries[r.source_entry_id].turns[r.turn_index].references.get("source_call_id")
                    for r in selected
                ],
                captured_payloads=len(
                    [r for r in selected if (r.session_id, r.turn_index) in observed]
                ),
                changed_contexts=[
                    r.request_id
                    for r in selected
                    if (r.session_id, r.turn_index) in observed
                    and r.session_id in expected
                    and any(
                        observed[r.session_id, r.turn_index].get(field)
                        != expected[r.session_id]["turns"][r.turn_index]["payload"].get(field)
                        for field in (
                            "messages",
                            "tools",
                            "model",
                            "max_tokens",
                            "temperature",
                            "top_p",
                            "stop",
                        )
                    )
                ],
            )
    atomic_json(directory / "quality.json", details)
    return results
