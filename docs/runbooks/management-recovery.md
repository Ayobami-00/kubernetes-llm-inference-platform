# Management lifecycle recovery

## Observe progress

Use Operations or a resource's Activity tab. Operations expose a stable ID, ordered persisted events,
errors and cleanup status. API polling is available at `/api/v1/operations/{id}`. An operation that
completed remains historical even if a resource later fails or expires.

## Worker restart

Jobs use transactional claims, 60-second leases and ten-second heartbeats. Checkpoints and provider
intents are persistent. Restarting a worker releases its session-level execution guards; an abandoned
claim becomes eligible after its lease expires. Another worker reconciles the provider ledger before
repeating an allocation. No database transaction remains open during provider calls.

Start another worker against the same database when testing concurrent processing:

```bash
docker compose -f dev/compose.yaml up -d --scale worker=2 worker
```

Actions on a resource are serialized. Retry API submissions with the same Idempotency-Key to receive
the same operation. Reusing a key with a changed request returns a conflict.

## Cleanup pending

Termination failure can exhaust five retries. The operation then fails with cleanup pending; member
identities remain visible and the group remains draining. Neither deleted nor expired is reported
until termination is confirmed. A failed create can also retain resources requiring cleanup.

For the simulation termination-failure preset, select the group, clear the simulated fault, then
choose Drain and delete again. This creates a new operation and preserves the original failure.
Successful cleanup clears outstanding cleanup flags while keeping historical operation errors.

For a future real provider, investigate allocation intent IDs and observed instance identities before
retrying. A timeout does not establish absence. Do not manually erase the provider ledger or force a
completed state to hide unresolved allocations.

## Lease expiry

The first confirmed allocation establishes allocated_at and expires_at. Recovery never resets them.
Renewal explicitly extends an active lease; draining groups cannot be renewed. The worker coordinates
expiry cleanup using PostgreSQL, so multiple workers do not create duplicate expiry actions.

TTL includes bootstrap and model preparation. The worker checks expiry again during allocation
progress. Expired capacity is drained and terminated before a terminal expiry state is recorded.

## Live Lambda refresh failures

The Capacity view shows the last successful observation and its timestamp. Refresh errors retain
that snapshot and mark it stale. Missing credentials show an unconfigured integration. Regional
availability is an observation, not a reservation. GPU runtime and model-serving readiness require
separate downstream evidence and are never inferred from account instance health.

## Database and logs

```bash
docker compose -f dev/compose.yaml ps
docker compose -f dev/compose.yaml logs --tail 100 api worker
```

API responses carry X-Request-ID. Logs omit credentials and provider response bodies. Back up the
PostgreSQL volume before schema upgrades or intentional resets. Alembic history is part of the
backend image; published revision and run snapshot protections are enforced by PostgreSQL triggers.

## Kubernetes execution recovery

Use Integrations to distinguish an unconfigured adapter, stale observations and blocked artifacts.
An import receipt records publication status separately from each file's availability. Restage a file
and import again with a new key to recheck it. Do not edit a frozen prepared bundle or historical run.

For a runner-control failure, inspect the run's persisted handle/UID and its shared attempt directory.
A Job with a different UID or manifest digest must not be adopted. Missing Jobs and abandoned
`started/` markers require an explicit new run, not clearing markers or restarting the old clock.

A cancellation writes a durable tombstone and waits for foreground deletion. Exhausted retries can
leave a failed operation with a nonterminal, cleanup-pending run. Request Stop again after resolving
connectivity/RBAC; do not mark it cancelled while Pods can still execute. Completed Jobs are retained
until terminal data is committed, then receive a one-day TTL. Artifacts remain on the shared PVC.

## Legacy development database backup and restore

This subsection applies to the single-instance development chart. The live lab
uses the three-instance `platform-data/klip-db` CloudNativePG deployment. Use
[the replicated database runbook](management-postgresql.md) for its WAL backups,
isolated restoration and failover tests; the commands below target the legacy
`klip/klip-db-0` instance.

Keep backups private and off the database PVC. The following uses the existing PostgreSQL Secret
through its container environment, without printing credentials:

```bash
umask 077
kubectl -n klip exec klip-db-0 -- sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h 127.0.0.1 -U postgres -Fc klip' \
  > /private/backup/location/klip.dump
```

Also back up the shared `/artifacts` filesystem, relevant Secret references and deployment versions.
Coordinate a quiet window or stop API writes and workers while taking a consistent database/artifact
backup. A database dump alone does not preserve external files or active Kubernetes Jobs.

Test a restore in a separate database without overwriting the live application:

```bash
kubectl -n klip exec klip-db-0 -- sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" createdb -h 127.0.0.1 -U postgres klip_restore_test'
kubectl -n klip exec -i klip-db-0 -- sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore -h 127.0.0.1 -U postgres -d klip_restore_test' \
  < /private/backup/location/klip.dump
```

Compare revision/run counts, frozen snapshots, import receipts and checksums before declaring the
backup usable. For disaster recovery, stop writers, provision storage, restore database/artifacts and
Secrets, and reconcile recorded external Job UIDs and provider intents before enabling workers.
Never point recovered workers at a different cluster or duplicate an ambiguous allocation.

The legacy development PostgreSQL chart uses one primary. Storage retention is not a backup or failover strategy.
Do not delete PVCs to resolve an application migration error. Inspect the migration hook and restore
a verified backup if a schema/data recovery is required. Changes to major PostgreSQL versions need
an explicit migration procedure; changing the image tag is insufficient.

After rotating the mounted administrator password, restart the API replicas so their cached password
verifiers reload it. Lambda observation workers read their mounted key on refresh. Changing the
PostgreSQL Secret alone does not change an initialized role's password; coordinate database role and
application Secret rotation before restarting clients.
