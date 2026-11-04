# WriterAssist on one A10: baseline versus n-gram speculative decoding

This is the versioned specification for the dashboard's fixed `a10-paired.v1`
experiment. Each execution appends measured results, exact frozen inputs and an
artifact index to its own immutable report. This file contains no assumed speedup.

## A. Question and hypothesis

Does n-gram speculation lower client completion latency for WriterAssist on the
retained A10, under identical inputs and admissions, with comparable correction
quality? Preserved token spans may provide cheap, useful proposals. Lookup and
verification overhead, short outputs and substantial rewrites may remove that
benefit. A regression or inconclusive result is a valid experimental outcome.

## B. Task and scope

WriterAssist corrects grammar and improves sentence fluency. The scope is one
externally retained A10 and Qwen3-4B, with non-streaming client requests. A CPU
runner Job sends requests through the authenticated private llm-d gateway and
endpoint picker to one vLLM model Pod. This does not test public DNS/TLS, serving
HA, other profiles or provider provisioning. The VM is never rebooted, replaced,
terminated or assigned an expiry by this workflow.

## C. Why JFLEG and why n-gram

[JFLEG](https://github.com/keisks/jfleg) is grammatical error correction and fluency
editing, **not summarization**. A correction can preserve contiguous input spans.
The pinned vLLM n-gram proposer searches token history for matching sequences and
proposes following tokens; the target model verifies those proposals. No separate
draft model consumes A10 memory. Accepted proposals may reduce decoding steps.

JFLEG also contains fluency rewrites, changed word order and tokenization effects.
Text overlap is a reason to investigate, not proof of useful proposals or speedup.
Prompt lookup is distinct from reusing cached prefixes across requests; prefix
caching stays disabled for both variants.

## D. Data and provenance

Development inputs are reserved for warmup; test inputs are scored. All four
correction references are retained outside inference payloads. The frozen manifest
records source revision, CC-BY-NC-SA-4.0 license evidence, preparation version,
source IDs, transformations, exclusions, tokenizer/template pins, input lengths
and real checksums. The prepared source is a public task proxy, not private
Grammarly or customer traffic.

Both child runs deliberately reuse the same selected cohort. Within either run,
selection is without replacement and warmup/evaluation are disjoint. Exhaustion
fails planning. Actual tested length distributions do not cover every input
allowed by the customer specification. Data and response artifacts stay outside
Git and are available only through authenticated management access.

## E. Environment and reproducibility

Freeze the actual node UID, boot ID, model/tokenizer commits, serving-image digest,
runner-image digest and software versions. Both profiles use BF16, context 768,
eight sequences, one GPU, GPU memory utilization 0.8, temperature zero, output cap
128, a two-second client timeout and `enable_thinking: false`.

Baseline disables speculation. N-gram sets `method: ngram`, five speculative tokens
and lookup bounds two through five. The effective upstream idle timeout must be
three seconds on **both** gateway replicas; accepted policy conditions alone do
not prove the translated runtime configuration.

## F. Controls and pairing

Keep model, image, tokenizer, hardware, templates, sampling, limits, routing,
authentication, caching, traffic, delivery policy and SLOs identical. Freeze source
contracts and admissions before execution. Compare machine-readable controls and
normalized schedules before calculating speedup. The single deployment is reserved
for the parent experiment. Baseline-first ordering is an explicit confounder.

## G. Timeline and duration

Each variant has 60 unscored warmup seconds followed by exactly 300 scored seconds:
180 steady at 0.5 requests/s, 60 burst at 1.5 requests/s and 60 recovery at 0.5
requests/s, with at most 30 seconds of drain. Poisson arrivals use seed 42. The
actual materialized count is reported and must support at least 100 eligible
requests. This is a distinct derived scenario; canonical configurations remain
unchanged.

Target approximately 18–21 minutes including activation, runner startup, exports
and analysis. Record actual stage durations and overruns. Never shorten scoring,
drop requests or relax targets to satisfy the time estimate. A separate 25-minute
safety watchdog records failure and stops further experiment stages.

## H. Measurement definitions

Client completion runs from actual send to complete usable response. Percentiles
use nearest rank: sorted sample at `ceil(p × n)`, with explicit successful-request
coverage. Preserve status, finish reason, incomplete output, usage provenance,
intended arrival, actual send and scheduling lag. Missing metrics stay unknown.
Non-streaming requests do not provide client TTFT. Server TTFT, queue and GPU
metrics are separately labelled diagnostics.

## I. Workload-delivery validity

Any missed due admission invalidates delivery. More than 1% of due admissions
lagging by over 100 ms also invalidates it. No hidden HTTP retries are introduced.
Execution status, measurement validity and performance verdict remain separate.
Invalid delivery cannot support a speedup claim or passing SLO assessment.

## J. Performance comparison

Report offered, eligible and successful requests; rejection, error, timeout,
cancellation and incomplete outcomes; latency p50/p95/p99; output lengths and
delivery diagnostics. Show absolute changes and `100 × (1 − ngram / baseline)`
percentage reductions with explicit units and denominators. Success-only latency
quantiles must never hide the all-request SLO denominator.

## K. SLO assessment

Preserve 95% completion within one second, fixed 300-second windows and minimum
100 eligible samples. Actual send time assigns requests to windows; later
completion remains in that send window. Eligible failures count against attainment.
Warmup and drain sends are unscored. Insufficient samples cannot pass. A completed
run may violate the SLO. Do not recalibrate targets in this experiment.

## L. Speculation diagnostics

Report observed proposal, proposed-token and accepted-token counters when exposed.
Acceptance is accepted/proposed tokens; accepted tokens per draft uses proposal
events as its denominator. Counter resets and HA metric deduplication must be
handled. Prometheus `increase` is a scrape-based estimate with boundary
extrapolation, not an exact per-request event count. Missing baseline series are
unavailable, not zero. High acceptance does not independently prove lower latency.

## M. Copy and edit analysis

Use the pinned tokenizer without added special tokens. Ordered, disjoint exact
matching blocks from `SequenceMatcher(autojunk=False)` define matching spans.
Matched/output tokens define copy fraction; report the longest block. Normalize
token Levenshtein distance by `max(source tokens, output tokens, 1)`. Empty-output
copy fraction is undefined. Matches cannot count the same source token twice.

These metrics compare the **source sentence** with output. System instructions
and chat-template tokens are excluded; full rendered input lengths come from
preparation metadata. Full-prompt overlap is not measured. Report source/output
length distributions and diagnostic overlap groups with sample counts. Aggregate
acceptance cannot establish per-request acceptance or causal subgroup effects.

## N. Quality comparison

Use JFLEG-compatible corpus GLEU with all four references. Show eligible, evaluated,
failed, missing and unscorable counts. Reconcile coverage. Additionally compare
the common successful/scorable cohort in sorted source-ID order, with reference
seed zero and 500 iterations. Record exact output agreement and retain a private
disagreement review bundle. A short pair and small GLEU difference do not prove
quality equivalence. No paid judge is used.

## O. Interpretation and uncertainty

State whether the measured pair supports the hypothesis, including quality and
failure tradeoffs. A descriptive paired bootstrap resamples matched source IDs;
record its seed, iterations and interval method. It does not provide independent
deployment repetitions. Order, warm state, temporal load, short outputs and small
sample sizes limit generalization. Retained security findings and time-limited
reviews remain visible. No universal speedup, production capacity or certification
claim follows from this experiment.

## P. Artifacts and reproduction

Open management → **Experiments**, review the fixed A10 preset, then select
**Run A10 baseline vs n-gram**. The durable parent owns two immutable child runs.
The browser and operator laptop may close. Cancel from the same view; completed
and partial child artifacts are preserved. An explicit rerun receives a new
identity. A lost child is failed, not automatically restarted.

Download the per-execution Markdown report, comparison JSON, definition, overlap
analysis and private disagreement bundle. Follow child links for snapshots,
preparation, admissions, normalized records, raw AIPerf exports and quality reports.
Artifacts are checksummed and verified before terminal completion; original
results and historical assessments are never rewritten. Grafana links use the
parent's recorded interval and the existing A10 diagnostics dashboard; exact child
timeline origins and scored boundaries remain in the stage and child reports.
