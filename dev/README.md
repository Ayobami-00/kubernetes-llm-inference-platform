# Local environments and runtime images

| Entry | Purpose |
| --- | --- |
| `start` / `compose.yaml` | Local management web/API/workers and PostgreSQL |
| `test-ui` / `compose.ui-test.yaml` | Isolated frontend/browser verification |
| `test-integration` | CPU-only Kubernetes integration proof |
| `inference-stub/` | OpenAI-compatible FastAPI stub with deterministic delay/failure cases |
| `host-executor/` | Restricted host executor image |
| `model-serving/` | Reviewed derived vLLM image and pinned patch inputs |

Run `./dev/start` from the repository root. It creates ignored secret files and
asks for an admin password. Open <http://localhost:5173>. Stop the local stack with
`docker compose -f dev/compose.yaml down`; volumes preserve local data.

Synthetic inference, emulated usage and simulated lifecycle operations are labelled.
Public-input stub runs still do not measure model performance. Cloud mutation and
real inference need their own explicit runtime configuration and readiness checks.

See [management development](../docs/development/management.md),
[workload development](../docs/development/workloads.md) and
[contributor checks](../CONTRIBUTING.md).
