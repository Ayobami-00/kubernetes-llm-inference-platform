# Management integration interfaces

KLIP accepts validated outputs from A and B while they develop independently. Use **Imports**
for a review before submission and **Integrations** for adapter configuration, artifact checks and
infrastructure history. Imported metadata is not evidence of live serving readiness.

## Document imports

All four endpoints require an administrator session, CSRF token and `Idempotency-Key`:

| Endpoint | `document` | Owner |
| --- | --- | --- |
| `POST /api/v1/imports/inventory` | `InfrastructureInventory` or the `foundation.v1` projection below | B |
| `POST /api/v1/imports/datasets` | `DatasetManifest` at an exact revision | A |
| `POST /api/v1/imports/prepared-runs` | `{id, fixture, snapshot: RunSnapshot}` | A |
| `POST /api/v1/imports/runs` | Terminal `Run`, including its frozen snapshot and validated result | A |

The request envelope is `{source, provenance, document}`. `source` identifies the exporter;
`provenance` is `synthetic`, `measured` (the source reports measurements), or `unverified`.
The UI accepts a document file and supplies this envelope. Payloads are limited to 5 MiB. Large
artifacts belong on the shared filesystem, not inside import JSON. Inline credential fields are rejected.

An accepted import returns `202`, `{import_id}`, and a `Location` for polling. A PostgreSQL-backed
job validates artifacts and commits the record. Request transactions commit before response headers
are sent, so immediate polling through another API replica sees accepted work. A receipt can complete with blocked artifact checks:
the history is still useful, but the inputs are not executable. Validation and immutable conflicts
are visible on the receipt. Reusing the same key and body returns the original receipt; changed
content under the same key conflicts. Repeated content under another key deduplicates by identity.

Imports never replace existing revisions or snapshots. A new dataset revision updates the catalogue's
current pointer but leaves existing references exact. Prepared bundles are immutable. Externally
completed runs create history only: importing one cannot launch a benchmark or lifecycle operation.
Fixture runtime documents must set `fixture: true` and cannot claim measured provenance. Unknown
fields are rejected by the committed shared contracts; coordinate contract changes before exporting
newer formats from A or B.

### Partial foundation observations

B's `foundation.v1` envelope contains `schema_version`, `captured_at`, `allocation`, `network`, and
`nodes`. `allocation.cluster_id` identifies the foundation; `network` and `nodes` use the shared
`NetworkInfo` and `NodeObservation` models. This is a management-specific projection, not a second
full inventory model. Only AWS control-plane observations are accepted in this projection.

A foundation import remains **partial**. It supplies no invented Kubernetes API CA, node readiness,
cluster identity or WireGuard state. Full and partial histories have separate current pointers per
cluster. Older captures remain historical and cannot displace a newer capture. Full non-fixture
inventory must come from B after verification.

## Prepared execution and preflight

A prepares the final `RunSnapshot`, including resolved revisions, bindings, admission schedule,
version pins, dataset checksums, declared windows and full inventory. A stages its prepared artifacts
before importing `{id, fixture, snapshot}`. C neither prepares datasets nor creates admission schedules.
Select the imported bundle in the run inspector. Its scenario and any explicitly submitted serving
bindings must match exactly; management stores the snapshot unchanged with separate runtime IDs.

`POST /api/v1/runs/preflight` reports separate checks for contract completeness, prepared source,
runner configuration, storage/checksums, current inventory, observed Kubernetes nodes, all readiness
stages, serving profile identity, managed capacity and sufficient remaining lease. Real submission
revalidates these checks, including `validate_for_execution`. Current real provider, host, cluster
mutation and serving adapters are unconfigured, so a simulated group cannot become real capacity.
A fixture bundle can only use explicit simulation. Live Lambda account instances are not managed
capacity and cannot satisfy these checks.

## Execute-only Kubernetes Job transport

Versioned adapter IDs are frozen into an immutable execution binding at submission. Legacy operations
migrate to `legacy-simulation-v1`; changing server settings cannot reroute accepted work. Bindings
contain image/configuration identifiers and credential references, never credential values.

- `simulated:v1`: existing persistent local simulator.
- `kubernetes-fixture:v1`: actual CPU Job running C's synthetic contract fixture builder; simulation only.
- `kubernetes:v1`: transport for A's execute-only image; requires real mode, a digest-pinned image and
  server-configured argv. Real readiness must still pass.
- `unconfigured:v1`: fail closed, with no fallback to simulation.

API, workers and runner Jobs mount the same RWX filesystem at `/artifacts`, with UID/GID 10001.
B provides production multi-node RWX storage. The kind proof uses a disposable single-node host mount;
it does not demonstrate production RWX storage or GPU performance.

Configure `runner.image`, `runner.command`, and optional `runner.secretRefs` in the application chart.
Commands are arrays, without a shell. Supported placeholders are `{snapshot_path}`, `{attempt_dir}`,
`{result_path}`, and `{operation_key}`. Credential names are server-selected Secret references,
mounted read-only under `/run/secrets/runner/<name>`. The runner service account has no API token or
Kubernetes permissions. A must supply Python 3 in its image for the small stdlib bridge.

Each attempt directory contains:

| File | Writer and meaning |
| --- | --- |
| `snapshot.json` | C; immutable canonical shared `RunSnapshot` |
| `manifest.json` | C; `management-runner.v1`, input digest, argv, identity, fixture flag and deadline |
| `started/owner.json` | Bridge; permanent atomic start claim with Pod UID and snapshot digest |
| `observation.json` | A; atomically replaced `{status: "running", stage: ...}` |
| `result.json` | A; atomically replaced shared `RunResult` on completion |
| `stdout.log`, `stderr.log` | Bridge; retained execution logs on the shared filesystem |
| `cancel-requested` | C; permanent cancellation tombstone |
| `bridge-complete.json` | Bridge; final child exit code |

Stages are `preparing`, `warming_up`, `measuring`, `draining`, and `finalizing`. A owns timing,
scheduling, measurements and SLO calculation. It must handle SIGTERM and write outputs atomically
(temp file, fsync, rename). Do not include credentials in runner logs or artifacts.

C persists a deterministic creation intent before calling Kubernetes. Ambiguous POSTs reconcile the
same Job and manifest digest; a persisted UID prevents adopting a replacement. `backoffLimit: 0`,
`restartPolicy: Never`, a deadline covering the declared windows plus startup allowance, and the
permanent filesystem claim prevent retrying a benchmark clock. A duplicate Pod waits for the original
attempt; an abandoned start marker never permits re-execution. Loss requires an explicit new run.

Recovery observes the same handle and UID. Completion requires the Job's terminal condition, no
active Pods, a valid `RunResult`, and validation in its owning shared `Run`. A successful process
without a valid result fails. Cancellation writes a tombstone, deletes the validated Job with a UID
precondition and foreground propagation, and waits for workload removal. Exhausted control retries
remain cleanup-pending and can be stopped explicitly. Job TTL is added only after terminal data is
committed; artifacts remain persistent.

## Artifacts and observations

Only registered `local://` or `file://` paths inside the configured artifact root are downloadable.
Checks stream SHA-256 and reject traversal, nonlocal file authorities, symlink escape and HTTP URLs.
Downloads recheck the checksum and use attachment responses. No arbitrary URL fetcher is exposed.
Import again under a new key to recheck staged files without changing frozen content. Real runner
preparation hashes execution artifacts again immediately before staging.

Workers cache read-only Kubernetes node and Deployment observations. GPU worker readiness requires
separate host/WireGuard/Kubernetes/Cilium/GPU evidence from B plus matching live node UIDs.
Serving Deployments must be available at their observed generation, carry label
`klip.dev/deployment-id`, and annotation `klip.dev/serving-profile` with the exact profile revision.
Observation age can block future runs; it never rewrites a historical verdict.

A owns benchmark execution and evaluation. B owns AWS, foundation configuration, bootstrap automation
and static releases. C owns management releases, Lambda lifecycle orchestration and eventual dynamic
model-pool releases. No AWS lifecycle permissions or competing bootstrap/evaluator implementation
are introduced here.
