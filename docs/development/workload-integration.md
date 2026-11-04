# Stream A integration with B and C

A runs preparation, planning, AIPerf and evaluation. C owns the management API, durable imports,
execution bindings and Kubernetes Job lifecycle. B owns capacity, cluster preparation, storage and
serving releases. There are no new Lambda mutations in this integration.

## Execution protocol

C selects `kubernetes-workload-stub:v1` for A's actual fixture-labelled local workloads, or
`kubernetes:v1` for real benchmarks. Its existing `kubernetes-fixture:v1` adapter remains a separate
synthetic contract test. The configured runner command is:

```json
["workload-runner", "execute", "--manifest", "{attempt_dir}/manifest.json"]
```

C stages a `management-runner.v1` manifest and snapshot under the shared artifact root. Its bridge
owns the permanent start claim. A checks the snapshot checksum and fixture identity, preserves the
snapshot bytes, uses C's run ID, and executes once. An interrupted attempt never starts a new clock.
`execution-identity.json` records C's operation/attempt ID and binding checksum.

A atomically publishes `observation.json` and `result.json`. The observation contains `status`,
`stage`, `result`, `error` and optional `timeline_origin_at`; C now uses the shared model. The origin
is the engine's paired monotonic/UTC origin, including warmup. `RunView.timeline_origin_at` exposes
it for correlation. C waits for Job completion and validates the complete run against its frozen
snapshot. Nonzero exits and cancellation retain partial results. A violating or invalid assessment
is distinct from an execution failure.

C verifies/registers result artifacts before its terminal transaction. The run inspector and
Integrations page offer downloads only for registered, checksummed files. Raw exports, normalized
records, the report and quality evidence are included. Collection does not change historical results.

The public control boundary remains launch/stop/observe with opaque, durable handles. C's worker
implements Kubernetes control in two stages: persist intent, then create/adopt the Job by identity.
Repeated API idempotency keys return the original attempt, including terminal attempts. Do not use
`LocalRunnerAdapter` across Pods: it is only for local commands sharing one filesystem/PID namespace.

For actual runner Jobs, terminal acceptance requires the saved completed observation,
its result and the original timeline timestamp to agree. Atomic file replacement on
shared storage can briefly expose different file generations to different Pods.
Management observes `finalizing` for up to 120 seconds after the original Job's
completion time, then fails inconsistent artifacts. The allowance is frozen with
the execution binding and cannot reset when a management worker restarts. The
dedicated contract-fixture adapter remains compatible with its simpler result protocol.

The native HTTP adapter queues diagnostic records for a background writer instead
of locking/flushing EFS on the request coroutine. Each process has a 32 MiB /
4,096-event bound, including in-flight writes, and batches for up to 25 ms before
an append/fsync. Immutable clock/context data are cached; session suppression uses
attempt-local scratch shared by AIPerf processes. The native HTTP/SSE implementation,
single timeline, timeouts and per-customer concurrency remain in use.

Every writer must produce a healthy final receipt with matching accepted/written
counts before evaluation. Overflow, storage failure, missing receipts or an
undrained writer fail the attempt and cannot produce a passing result. The
checksummed `journal-status.json` documents this policy. A hard Pod loss can lose
pending events from local memory; previously flushed partial records remain on
EFS, the attempt fails, and execution never restarts automatically.

## Artifacts, credentials and resources

Use the same `/artifacts` mount in preparation, management API/worker Pods and runner Jobs. Stage
prepared files and the admission schedule there before freezing. Do not rewrite host paths at launch.
Planning also writes `management-bundle.json`, an import envelope for C's prepared-runs endpoint.
Derived bundles remain selectable from their original scenario through `source_scenario_ref`.

Credential references in snapshots map customer IDs to environment variable names. C mounts
allowlisted Secrets at `/run/secrets/runner/<secret>/<key>`. Append a server-configured argument:

```text
--credential-file INFERENCE_TOKEN=/run/secrets/runner/inference-access/token
```

A resolves it only during execution. Secret values stay out of arguments, snapshots and retained
exports. Redaction never rewrites frozen inputs. Execution rejects credential values embedded in
those inputs. Credentials in endpoint/import URLs are rejected.

Configure `runner.resources` in Helm (or `MANAGEMENT_RUNNER_RESOURCES` outside Helm). CPU/memory
requests must be positive and no greater than limits. C freezes these with its execution binding;
changing settings only affects new bindings. Size them against B's CPU capacity and measured
scheduling lag. The workload delivery policy remains strict; low resource reservations never justify
silently relaxing it.

## B: external capacity handoff

First import a full measured inventory. Then submit the typed document to
`POST /api/v1/imports/external-capacity`, using the same admin/CSRF/idempotency conventions as other
imports. Its JSON shape is documented by `ExternalCapacityImport` in management OpenAPI:

- Envelope: `source`, `provenance: measured`, `document`.
- Document: immutable `id`, imported `inventory_id`, `worker_groups`, `deployments`.
- Each group: runtime `id` and the shared non-fixture `WorkerGroup` containing its exact spec,
  observed members, provider IDs, Kubernetes node identities and actual lease.
- Each deployment: runtime `id`, `group_id`, pinned shared `profile`, `kubernetes_uid` and full
  inference `endpoint`.

GPU inventory nodes use `managed_by: external`. Existing foundation/management observations remain
compatible. Imported profile/group revisions are immutable and appear in the catalogue; an existing
working draft or conflicting revision blocks the import. New observations use a new handoff ID.
Replaying an existing handoff does not roll resources back.

These resources have `mode: real`, `ownership: external`. C lists/selects them, but rejects renewal,
deletion, simulation faults and serving lifecycle changes. Expiry blocks new launches without
claiming that B's instances terminated. B refreshes inventory and reimports observations when needed.

Readiness separately requires fresh inventory/live cluster observations, matching member/node UIDs,
allocatable GPUs, remaining lease and a ready deployment at its current generation. Deployment
metadata must include:

```text
klip.dev/deployment-id: <runtime deployment ID>                  # label
klip.dev/serving-profile: <exact profile ID>@<revision>           # annotation
klip.dev/serving-profile-sha256: <normalized profile digest>      # annotation
```

The profile digest is SHA-256 of `json.dumps(profile.model_dump(mode="json"), sort_keys=True,
default=str).encode()`, after validating with the current shared `ServingProfile`. The deployment's
image must exactly match the pinned profile image, and its UID must match the handoff. Direct-vLLM
runs must also match the handoff endpoint. Declarations alone never make a run executable.

## Workspace and generated interfaces

Use `uv sync --locked --all-packages --all-extras` for development/integration. The management
production container still installs only management dependencies while staging all workspace members.
The runner image includes pinned AIPerf 0.13.0. Shared schema version stays `0.1`, with compatible
defaults for serving stack, chat-template settings and observation metadata.

Regenerate with `scripts/export-contracts.py`, `scripts/export-management.py` and the web package's
`npm run generate`. Merge these source/interface changes into B/C before producing new handoffs or
releasing the matching API and runner images. Existing bindings retain their original configuration.

See [live validation](../runbooks/live-inference-validation.md) for the gated GPU procedure and
[workload recovery](../runbooks/workload-recovery.md) for local attempts.
