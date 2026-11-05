# On-demand WriterAssist baseline versus n-gram

The reference AWS environment was [decommissioned on 5 November 2026](full-project-decommission.md).
The hosted dashboard is offline. The instructions below require a newly deployed,
verified environment; historical resource identities must not be reused as readiness evidence.

Use management → **Experiments** at <https://management.klip-lab.click/experiments>.
Expand the frozen preset summary, review it, then choose **Run A10 baseline vs
n-gram**. One durable parent reserves the deployment and creates two ordinary
management-owned runner Jobs. Closing the browser or operator terminal does not
stop execution. The comparison targets approximately 20 minutes with healthy,
cached serving startup; the view reports actual elapsed time.

The two runs select the same public JFLEG records and admission offsets. The short
derived scenario has 60 seconds warmup, 300 scored seconds and at most 30 seconds
drain per variant. Canonical scenarios, the two-second request timeout and the
one-second/95% customer SLO stay unchanged. The model deployment changes only
between the two pinned variants. The retained VM, driver and toolkit are untouched.
Both generator Jobs are pinned to `platform-01`; the parent freezes its observed
Node UID and CPU/memory reservations. Management itself retains its HA placement
across CPU workers. A changed/unavailable client node blocks the comparison.

## Initial deployment

Use the existing retained-capacity onboarding and public JFLEG preparation first.
Stage the already cached pinned tokenizer for CPU copy/edit analysis:

```sh
uv run --locked --all-packages --all-extras python \
  scripts/infrastructure/stage_a10_analysis.py \
  --evidence artifacts/acceptance/manual-a10-20261104
```

This computes real checksums and stages immutable files on the shared volume. It
does not download a tokenizer, rewrite prepared examples or change a VM. A changed
tokenizer needs a new preparation/profile identity; do not overwrite the cache.

The retained management overlay enables `experiments.a10Enabled` while keeping
provider lifecycle disabled. Other installations default to disabled. Build and
publish the backend, web and runner from the same reviewed commit, collect private
image receipts, and use the existing `management_release.py` prepare/deploy flow
with `--external-capacity-only`. Never hand-edit the live Deployment image.

The feature uses the existing schema `0.1` with additive experiment operation
types. No database migration is required: existing resources, operations, durable
jobs, execution bindings and artifact registrations hold the workflow.

Only management workers receive the inference probe's mounted Secret file and a
namespaced Role to patch `llm-d/writer-assist-a10`. API and benchmark runner service
accounts receive no new Kubernetes control permissions. Gateway observation uses
the checksum-pinned kubectl with limited Pod port-forward permission in `llm-d`;
admin responses stay in memory and only fixed upstream timeout evidence persists.
The worker can observe Cilium Pod readiness and read private Thanos diagnostics.
There are no provider, SSH, driver or host-runtime mutation capabilities in the
actuator. General external-resource lifecycle APIs remain prohibited.

## Readiness and outcomes

Launch requires the fixed external node/deployment identities, prepared artifacts,
tokenizer checksums, pinned runner and valid time-limited security reviews. Before
each child, the worker checks the retained boot/Node UID, GPU allocatable capacity,
model digest/arguments, Cilium readiness, EPP/proxy pins, both effective gateway
timeouts and authenticated connectivity. Failure blocks execution; no replacement
GPU is rented and no benchmark is silently replayed.

Execution state, delivery validity, performance verdict and GLEU quality remain
separate. Invalid delivery cannot support a passing SLO or speedup claim. A
completed experiment can show a latency regression or violating SLO. Missing
token/TTFT/speculation measurements remain unknown. The non-streaming client has
completion latency, not client TTFT.

## Cancellation and recovery

Select **Cancel experiment** to stop further stages and cancel an active child
through the existing runner protocol. Partial records and completed children are
retained. The VM stays running. A repeated idempotency key returns the original
operation, including after completion. A separate rerun gets a new identity.

API/worker restart reconciles persisted child identities and existing Jobs. If a
worker died just after child admission, it observes that child without injecting
new probe traffic or creating a new plan. Model activation is idempotent and uses
an ownership annotation and resource-version check. Original execution clocks and
snapshots are never rewritten. A lost child is terminal failure, not an invitation
to restart. An unsafe model container restart stops the experiment-owned serving
deployment rather than entering a retry loop.

Measured planning inputs are frozen before compilation. A crash after writing a
plan but before database admission reuses that inventory timestamp and readiness
record. Recovery still requires the same serving Pod, GPU/CPU boot identities and
fresh evidence; stale inputs fail normal preflight instead of being rewritten.

If cleanup cannot contact Kubernetes, the deployment reservation remains until
cleanup can be verified. Inspect the existing parent/Job; do not clear the lock or
launch a replacement. Normal operator model releases also refuse an active
dashboard ownership annotation.

## Reports and private diagnostics

The history card links both child run records and verified downloads for the
Markdown report, comparison JSON, definition, token overlap and private disagreement
bundle. Child result artifacts contain schedules, snapshots, normalized records,
raw exports and quality reports. Parent files live under
`/artifacts/experiments/a10-pairs/<experiment-id>/`.

Analysis freezes its inputs and calculation before writing final report files, so
worker recovery reproduces the original bytes. Historical reports and assessments
remain immutable. The experiment definition/report specification is
`experiments/writer-assist-a10/ngram-comparison.md`.

An opt-in Playwright procedure exercises the actual deployed dashboard. It never
provisions capacity or submits an experiment through a hidden API shortcut:

```sh
node scripts/infrastructure/a10_comparison_ui.mjs inspect artifacts/acceptance/a10-pair-preview
node scripts/infrastructure/a10_comparison_ui.mjs launch artifacts/acceptance/a10-pair-launch
node scripts/infrastructure/a10_comparison_ui.mjs collect artifacts/acceptance/a10-pair-result <experiment-id>
```

Use a fresh output directory for each action. A saved launch intent must be
observed rather than resubmitted. `cancel` takes an existing experiment ID and
clicks that card's cancellation button. Downloads are checksum-verified; captured
responses and screenshots remain in ignored private artifact storage.

Use the existing private access helper for Grafana, then open the A10 speculation
dashboard. Server counters and scrape-based increases explain behavior; client
records are the SLO authority. A successful pair establishes behavior on this
tested private deployment, not serving HA or a production certification.
