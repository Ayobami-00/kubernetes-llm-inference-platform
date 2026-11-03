# Stream A integration notes for B and C

The workspace adds `workload-runner`, `slo-evaluator` and `inference-stub`. The root lockfile includes
optional AIPerf 0.13.0 and data dependencies. Normal `uv sync --locked --all-packages` keeps AIPerf
optional; execution hosts/images additionally install the runner's extras. The management backend
Dockerfile copies the new workspace members so its locked workspace installation still resolves.

Shared schema version remains `0.1`. Existing fixtures still validate. New compatible snapshot
fields carry target, preparation, delivery policy and lineage. New schemas cover prepared entries,
admissions, normalized requests/sessions and runner observations. Token counts may now be null when
unknown. C's generated OpenAPI/client are regenerated from real routes. The UI renders unknown
usage explicitly instead of crashing or presenting unknown values as measured zeroes.

## C: runner protocol

```python
from pathlib import Path
from workload_runner.control import LocalRunnerAdapter

runner = LocalRunnerAdapter(Path("/durable/workload-attempts"))
handle = runner.launch(snapshot, operation_key="management-operation-id")
observation = runner.observe(handle)
# observation.status, observation.stage, observation.result, observation.error
runner.stop(handle)
```

`snapshot` is the shared `RunSnapshot` produced by planning. `handle` is opaque to C. For this local
adapter it resolves to an attempt directory; management stores it unchanged. `launch` returns
quickly after durable creation and process launch. Repeated identical operation keys reuse the
attempt without new preflight or credentials checks; conflicting inputs fail. `observe` returns
the existing management observation shape and detects lost attempts. Results validate against the
same `RunResult` used by C. Management's simulated adapter remains unchanged; API/DB wiring stays
with C. The local process adapter does not introduce another HTTP API.

Use a stable durable artifact root on one runner host, with the same path and PID namespace for
launch/observe/stop. File locks serialize launch and observation updates. This local adapter is
not a distributed job scheduler. C can later supply Kubernetes-job submission behind the same
protocol; B owns the Job/Helm infrastructure. See the recovery runbook for liveness behavior.

Preparation and compilation happen before the snapshot freezes. C may invoke the Python planner
or CLI in its durable preparation job, then submit the frozen snapshot. Endpoint credentials are
environment variable names keyed by derived customer IDs. The planner maps authored customer IDs
to those derived IDs. Secret values are resolved only during execution and are excluded from
snapshots/retained exports. Never put credentials in endpoint URLs.

## B: packaging and storage

The runner's container entrypoint is the CLI, suitable for a later Job. Mount stable, writable
artifact paths, including prepared input files referenced by `file://` URIs. Set paths before
freezing; launch never rewrites its snapshot. SIGTERM requests cancellation and retains partial
records. Give the Job termination grace enough time for its 10-second engine shutdown allowance
and artifact finalization. Public preparation is a separate network-enabled step; execution
itself doesn't download datasets. Benchmark preflight still requires verified infrastructure and
serving pins. Stream A makes no infrastructure or provider calls.

Regenerate shared schemas with `scripts/export-contracts.py`; regenerate C's OpenAPI using
`scripts/export-management.py`, then run `npm run generate` in the web package. These remain
generated outputs of existing models/routes, not placeholder API definitions.
