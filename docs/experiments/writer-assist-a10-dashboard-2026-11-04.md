# Dashboard A10 paired comparison — 4 November 2026

The first dashboard-triggered paired experiment completed on the retained A10.
It supports testing n-gram prompt lookup further: client p95 completion latency
fell by **20.44% in this pair**. This is one baseline-first deployment comparison,
not a universal speedup or evidence of quality equivalence.

## Execution and controls

- Parent: `experiment-7d1b94581a648a4d9e74`.
- Operation: `op-dcb0a01cf30248d3`.
- Baseline: `run-6dc740470bbd8bfc`.
- N-gram: `run-b6532b3988cc8701`.
- Dashboard action: authenticated **Experiments → Run A10 baseline vs n-gram**.
  The browser closed after launch; management and its runner Jobs continued.
- Created: `2026-11-04T17:21:37.972329Z`.
- Terminal: `2026-11-04T17:39:27.143560Z`.
- Total terminal wall time: **1,069.17 seconds (17m 49s)**. Report calculation
  completed at 1,068.75 seconds; the remaining fraction is artifact registration
  and terminal persistence. No scoring interval was shortened to meet the budget.
- Both variants: 60-second warmup, 300 scored seconds, bounded drain, Poisson
  admissions and seed 42. The materialized schedule has **34 warmup and 220
  scored requests**, without replacement within a variant.
- Both runner Jobs used the same pinned CPU worker, `platform-01`.
- Both gateway replicas' effective upstream idle timeout was verified as three
  seconds before execution. Authentication probes returned missing/wrong key
  401, unknown model 404 and valid request 200. No HTTP retries were added.

The model remained Qwen/Qwen3-4B at model/tokenizer revision
`1cfa9a7208912126459214e8b04321603b3df60c`, using the reviewed pinned vLLM image.
Controls were BF16, context 768, maximum sequences 8, GPU utilization setting
0.8, non-thinking output, prefix caching disabled, temperature zero and output
cap 128. The only serving difference was n-gram lookup with five proposed tokens,
minimum lookup two and maximum lookup five. There was no separate draft model.

## Data

JFLEG is grammatical-error correction and fluency editing, **not summarization**.
The public corpus is a task proxy, not private customer traffic. Prompt lookup
may reuse unchanged contiguous token spans, while rephrasing can reduce matches.

Preparation `prepared-writer-assist-a33798fa6fa5` retains all four references,
outside inference payloads. Its 100 development records supply warmup and 747
test records supply evaluation. Both variants deliberately reuse the same
selected inputs and offsets; warmup and evaluation cohorts are disjoint.

- Source revision: `ee06ff806a208aba815ac45313f4e750a48330a5`.
- License: `CC-BY-NC-SA-4.0`.
- Prepared entries SHA-256:
  `a76e48a11c2c559352e735fd64be31f3721453707c771fde58eef7d0e5bf8f3a`.
- Normalized paired schedule SHA-256:
  `d03c915cb1a5f064ac6d87fe854b09d2eeae1714c9f9d4b1eb1568b1e1a7cf61`.
- Selected scored rendered input lengths: 35–101 tokens, median 50, p95 70.
  These short inputs do not cover every permitted customer input length.

## Results

| Measurement | Baseline | N-gram |
|---|---:|---:|
| Execution | completed | completed |
| Workload delivery | valid | valid |
| Scored requests / successful outputs | 220 / 220 | 220 / 220 |
| Rejections, errors, timeouts, missed admissions | 0 | 0 |
| Admissions delayed above 100 ms | 0 | 0 |
| Maximum admission lag | 3.22 ms | 4.08 ms |
| Client completion p50 | 527.18 ms | 377.45 ms |
| Client completion p95 | 853.79 ms | 679.30 ms |
| Client completion p99 | 1,083.17 ms | 961.24 ms |
| Completed within one second | 214/220 (97.27%) | 219/220 (99.55%) |
| Fixed-window SLO verdict | passing | passing |
| Comparison GLEU | 0.426734 | 0.425763 |
| Quality evaluated / eligible | 220/220 | 220/220 |

Percentiles use nearest rank. Each assessment has one full 300-second window,
minimum 100 eligible samples and the unchanged 95% one-second completion target.
All eligible failures would count against attainment. Non-streaming requests
provide client completion latency; **client TTFT is unavailable**.

Comparison GLEU uses the same sorted source-ID ordering, seed zero and 500 reference
sampling iterations. This ordering may differ from a child's standalone quality
report; retain each original assessment rather than overwriting it. Here the
common successful cohort is all 220 inputs. Outputs are exactly identical on
210/220 inputs. The ten disagreements remain in a private downloadable review
bundle. The small score difference does not establish quality equivalence.

The descriptive mean paired latency delta is −133.09 ms, with request-pair
bootstrap 95% interval [−146.12, −120.08] ms (1,000 iterations, seed 42). This
conditional interval is not repeated deployment-level evidence.

## Server diagnostics and copy/edit analysis

Deduplicated reset-aware Prometheus increases over the n-gram scored interval
estimate 14,482.76 proposed and 1,647.93 accepted tokens across 2,896.55 draft
events: **11.38% acceptance**, or **0.569 accepted tokens per draft**. Fractional
values are scrape-edge extrapolation estimates, not exact per-request counters.
Baseline speculation counters remain unavailable. Acceptance alone does not prove
speedup, and these aggregate metrics cannot establish per-request causation.

Pinned-tokenizer ordered disjoint matching blocks measure copied source-sentence
tokens relative to output tokens; normalized Levenshtein distance measures edit
extent. Source sentences exclude system/chat-template tokens. Full rendered
length is recorded separately; full-prompt overlap is not measured.

| Baseline source-copy fraction | Paired inputs | Mean latency delta, n-gram minus baseline |
|---|---:|---:|
| Below 50% | 12 | −0.57 ms |
| 50–80% | 124 | −104.44 ms |
| At least 80% | 84 | −194.31 ms |

These exploratory groups are compatible with the copy-span hypothesis; the small
low-overlap group, fixed order, warm state and differing outputs limit inference.
Prompt lookup here is distinct from cross-request prefix caching, which is off.

## Observed stage timing

| Stage | Seconds |
|---|---:|
| Initial preflight | 0.66 |
| Baseline activation mutation | 0.04 |
| Baseline readiness and planning | 138.77 |
| Baseline child, including startup/export | 384.51 |
| N-gram activation mutation | 0.04 |
| N-gram readiness and planning | 149.72 |
| N-gram child, including startup/export | 377.12 |
| Comparison calculation/diagnostics | 2.75 |

Polling gaps, registration and terminal persistence are included in the total,
so the stage rows alone do not sum to terminal wall time. Client timing origins
were `17:24:21.508378Z` and `17:33:13.955994Z`; each scored interval begins 60
seconds later and lasts 300 seconds. Runner Job UIDs are retained in the parent
report, separate from serving Pod identities.

## Access, reproduction and limits

The hosted management endpoint went offline with the
[5 November AWS decommission](../runbooks/full-project-decommission.md).
Public readers can use the [aggregate results and Matplotlib figures](../../experiments/writer-assist-a10/README.md).
Before decommissioning, the Markdown report, comparison JSON, frozen definition,
terminal record, overlap rows and private disagreements were downloaded through
management and SHA-256 verified. Child snapshots, schedules, normalized records,
responses, raw AIPerf exports and assessments were preserved in the operator's
encrypted local recovery archive. Private outputs are not published in Git.

The generated report SHA-256 is
`f9aeb38b5488767865ba8f1acee7b95d9f3f82fb0d97fbe0983242032891a8f9`;
comparison JSON is
`79a7719d21054424610034a55fe3b91fc2343b234c3b9dcf9fa0111ff6e33f9a`.
Raw data, captured responses, screenshots and execution files stay outside Git.

## Recovery and cancellation acceptance

A separate dashboard attempt, `experiment-7c790335526b3e7163c3`, tested control
behavior rather than contributing performance evidence. One UID-guarded management
API Pod replacement at `17:46:19Z` and one management worker Pod replacement at
`17:47:20Z` each restored two ready replicas on different CPU hosts. Throughout,
the original child `run-abf6af5a0eae4e9f` retained Job UID
`dc89cfc7-b0da-469b-ba22-45847303a5ba`, the exact snapshot and timeline origin
`17:46:00.812126Z`. There was no duplicate child or reset execution clock.

Dashboard cancellation then ended the child at `17:47:45.925326Z` and parent at
`17:47:46.425012Z`, preserving a partial result. No n-gram child was admitted.
This deliberately interrupted attempt makes no paired performance claim.

All 48 artifacts of the completed pair were downloaded through authenticated
management, checksum-verified and checked for the known admin and inference
credentials; neither value was present. Local checks passed 1,222 Python tests
(17 opt-in tests skipped), 21 frontend tests, Ruff/formatting, schemas, OpenAPI
and generated-client drift checks. Deployment uses scanned immutable image
receipts; retained vulnerability reviews remain explicit limitations.

For another pair, review the preset and explicitly launch a new identity. The
specification is [ngram-comparison.md](../../experiments/writer-assist-a10/ngram-comparison.md); operations and recovery
are in [the runbook](../runbooks/dashboard-a10-comparison.md).

This private single-A10 experiment retains explicitly reviewed security findings
with short expiries. It is not production-certified or a serving-HA demonstration.
The experiment does not own a GPU lease or VM shutdown operation. No additional
GPU capacity was rented and no VM/runtime/driver replacement was performed.
