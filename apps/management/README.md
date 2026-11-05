# Management application

KLIP is the authenticated web control surface for configurations, capacity,
operations, workload runs and experiment reports.

| Directory | Responsibility |
| --- | --- |
| `web/` | React/TypeScript UI, topology, specifications, capacity, runs, experiments and generated API client |
| `backend/src/platform_management/api/` | FastAPI routes, administrator sessions, CSRF and typed imports/actions |
| `backend/src/platform_management/db/` | PostgreSQL models, seeds, immutable revisions, bindings and durable history |
| `backend/src/platform_management/jobs/` | Claimed/checkpointed work, runner Job ownership, recovery and paired experiments |
| `backend/src/platform_management/integrations/` | Kubernetes, runner, artifact, serving and host-operation boundaries |
| `backend/src/platform_management/providers/` | Lambda observations, guarded mutations, provider ledger and simulation |
| `backend/src/platform_management/host_executor/` | Separately deployed restricted host executor; SSH custody stays outside management |
| `backend/migrations/` | Alembic database migrations |

From the repository root, `./dev/start` starts the local Compose stack and prompts
for an admin password. Open <http://localhost:5173>. Default lifecycle actions are
simulated; live operations need explicit configuration and readiness evidence.

The deployed A10 experiment uses ordinary management-owned runner Jobs and a fixed,
reviewed serving actuator. Externally retained GPU resources keep their lifecycle
protection. Benchmark Jobs receive no provider or SSH credentials.

See [local development](../../docs/development/management.md),
[integration contracts](../../docs/development/management-integrations.md),
[deployment](../../docs/development/management-deployment.md) and
[the paired experiment](../../docs/runbooks/dashboard-a10-comparison.md).
