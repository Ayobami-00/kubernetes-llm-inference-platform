# Workload attempt recovery

Inspect `run.json`, `heartbeat.json`, `engine.log` and the readable report in the attempt directory.
Execution status, measurement validity, SLO verdict and quality are separate outcomes.

Request cancellation with `workload-runner stop <attempt-directory>`. Stop is idempotent for
terminal attempts. The runner stops admissions, terminates its engine and retains partial
observations. SIGINT/SIGTERM request the same cancellation. The process supervisor allows a
bounded native shutdown, then kills its owned process group if necessary.

`LocalRunnerAdapter.observe(handle)` checks process ownership and heartbeat freshness. A missing
process or heartbeat older than 30 seconds fails an active attempt with `lost_attempt`. It never
restarts it with a new clock. Retain the directory for diagnosis and launch a new attempt with a
new operation key after fixing the cause. Reusing the old key always returns the old attempt.

The startup watchdog allows 120 seconds beyond the workload budget; engine export/shutdown has a
separate 60-second allowance beyond the measurement/drain timeline. Native HTTP deadlines enforce
the drain budget. An export timeout is an execution failure, not a customer SLO violation.

Do not edit snapshots, schedules, records or preparation files to repair a historical run. Artifact
checksums make such changes fail preflight/reassessment. Produce new preparations and plans with
new identities, then run a new attempt. `evaluate` creates a new assessment under the original
attempt while preserving its historical result.

Keep the durable artifact root and preparation cache on storage surviving the runner process.
The local adapter relies on POSIX file locks and a shared PID namespace; it is a single-host process
adapter. Management replicas should submit through one runner host or use a future job adapter
implementing the same protocol. Sharing only a filesystem across unrelated PID namespaces is
insufficient for liveness checks. C owns that deployment and persistence integration.
