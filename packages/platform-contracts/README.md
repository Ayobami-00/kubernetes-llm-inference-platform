# Platform contracts

`platform_contracts` is an installable library with Pydantic v2 models, YAML loading, local reference
resolution, snapshot construction and offline validation. Its runtime dependencies are Pydantic and
PyYAML. It imports no service, cloud, Kubernetes, benchmarking or persistence libraries.

## Interfaces

| Interface | Meaning |
| --- | --- |
| `CustomerSpec` | Service tier, workload, request generation, limits, allowances, SLO and quality intent |
| `ScenarioSpec` | Participants, warmup, phased admission traffic, seed, faults and drain timeout |
| `WorkerGroupSpec` | Desired Lambda instances, expected hardware, worker class and finite lease |
| `WorkerGroupStatus` / `WorkerGroup` | Observed members, counts, allocation and expiry; optionally wrapped with the spec |
| `ServingProfile` | Aggregated vLLM model recipe, resources, placement and routing intent |
| `OperationStatus` | One background management action and its public progress |
| `InfrastructureInventory` | Observed cluster, network, host and GPU information |
| `DatasetManifest` | Public source and preparation metadata; optional prepared-artifact metadata |
| `RunSnapshot` / `Run` / `RunResult` | Resolved inputs, execution state, and final measurements/assessments in one run resource |

`load_catalog(root)` loads all five configuration categories and rejects duplicate identities,
missing dataset references, incompatible interactions and traffic outside the customer allowance.
`catalog.get(kind, "id@revision")` requires an exact revision; it never falls back to a newer one.
`load_document(path, Model)` safely loads one YAML document and rejects duplicate mapping keys.

`build_run_snapshot(catalog, scenario_ref, bindings, ...)` resolves all participants, dataset manifests,
serving profiles and worker groups. It includes defaults and makes an independent copy of inputs,
versions, inventory and schedule metadata. Persist the snapshot once before execution; the management
backend owns database write protection and published-revision enforcement. Editing a
published configuration requires a new revision rather than rewriting historical inputs.

`validate_for_execution(snapshot)` checks that draft placeholders have been resolved: prepared
datasets and their tokenizer/template identity, source revisions and reviewed licenses, immutable
model/tokenizer/image pins, concrete provider selections, version metadata, non-fixture inventory,
checksummed admission schedule and a minimum lease budget. It performs no I/O. It does not prove
that referenced artifacts exist, that images are compatible, that a model fits GPU memory at runtime,
that hosts are reachable, or that a serving endpoint is ready. Bootstrap/load time and remaining lease
headroom require backend checks before execution.

Commit identifiers for model/tokenizer pins are full 40- or 64-character lowercase hex values;
container pins use `repository@sha256:<64-character digest>`. Dataset source revisions can also be
immutable release identifiers. A prepared manifest's `prompt_template_sha256` must match
`customer.workload.prompt_template_digest`, which hashes the canonical template/replay description.
The preparer must also record a real artifact checksum; this package does not read artifact contents.

## Fixed v0.1 semantics

- Schema version is `0.1`; all authored records have stable kebab-case IDs and positive integer
  revisions. Unknown fields, non-finite values and invalid units/counts are rejected. Timestamps
  require an explicit offset and serialize in UTC.
- Customer limits apply to the fully rendered input, including templates and tool schemas. The
  declared input limit plus output allowance must fit the serving context; actual tokenization is
  the preparation/runner's responsibility. Output limits are ceilings, not forced output lengths.
- Single requests and session admissions have separate traffic units. All session calls are ordered
  and response dependent. CodeAgent preserves recorded call contexts; responses are not fed into
  later snapshots and tools are not executed. An admission schedule records planned request or
  session starts; later session calls also depend on observed response completion.
- Warmup is unscored. Scored duration is the sum of phase durations; drain follows measurement.
  The runner must reserve disjoint warmup/evaluation records and fail before execution if the
  selected data cannot satisfy the materialized schedule without replacement. No arrival scheduler
  or dataset sampler is implemented here.
- Allowances describe admission envelopes, not measured hardware capacity. Consecutive phases
  above the sustained allowance share one burst-duration budget. Provisioned customers request
  explicit reserved concurrency; the future admission layer must enforce that reservation.
- Each binding names an actual deployment identity. Customers reusing that identity share one
  serving profile and worker group. GPU accounting counts replicas once per deployment. A dedicated
  customer requires an exclusive worker group. GPU accounting checks total capacity and whether
  each replica fits within one instance; Kubernetes still determines actual placement.
- The engine has an aggregated topology only. `load_aware` and `cache_aware` name platform routing
  intentions, not upstream CLI flags. Optional n-gram speculation is a typed recipe, not a claim that
  any unpinned image supports it. Engine adapters, tenant authentication, trusted cache namespaces
  and routing enforcement remain deployment work.
- Worker TTL starts at the first confirmed allocation, includes bootstrap/model loading, and does
  not reset on retry. Renewal is explicit. `expired` is a final observation with termination
  confirmed; use `draining` during cleanup. Manual deletion uses `deleted` only after all members are confirmed terminated. GPU-ready is distinct from model-serving-ready.
- Member identities include the provider instance ID and, after joining, the Kubernetes name and
  UID. Allocatable GPUs are distinct from allocated GPUs and provider availability.
- Operation `completed` means the requested action finished, including required cleanup. A failed
  operation can still have cleanup pending. Later resource failure does not rewrite historical
  operation success. Durable claims, idempotency and reconciliation are backend implementation work.

## Results and SLOs

Run status is `queued`, `running`, `completed`, `failed` or `cancelled`. Only a running run has an
active stage (`preparing`, `warming_up`, `measuring`, `draining`, `finalizing`). A lost runner ends
its execution attempt; backend recovery must not silently restart a benchmark clock.

Each terminal result includes aggregate and per-customer request counts. Completed, rejected,
failed, timed-out and cancelled outcomes partition attempted requests; attempted plus not-sent
arrivals equals offered requests. An incomplete offered schedule makes measurements invalid.

SLO eligibility uses original scored requests with one HTTP attempt each in v0.1. In-contract
rejections, errors and timeouts count against attainment; exclude warmup and explicitly out-of-
contract requests. Record cancellation reasons in detailed request artifacts. Do not remove slow or
failed requests to improve an SLO. Latency starts at actual client send; planned admission times and
client scheduling lag are separate measurements required from the future runner.

Every customer/metric assessment covers all fixed, non-overlapping windows in the measurement
period and accounts for every eligible customer request. Too few samples produces
`insufficient_data`. Window verdicts are checked against supplied counts and thresholds; thresholds
must match the frozen customer contract. These are consistency checks, not a raw-event SLO evaluator.
This package does not calculate request eligibility, latency quantiles, scores or actual measurements.

Measurement validity, execution outcome, SLO verdict and quality are independent. A completed run
can violate its SLO. Invalid measurements have no SLO assessments and use `not_evaluated`.
Unavailable latency measurements are omitted, not represented as zero. Quality can remain
`not_evaluated`; CodeAgent replay cannot claim a functional agent success score.

Final results remain historical; a dashboard may mark live observations stale without changing a
completed result's verdict. Large raw exports, request records, faults, cost reports and deployment
events can be linked in `result.artifacts` instead of embedding them in the run row.
