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
