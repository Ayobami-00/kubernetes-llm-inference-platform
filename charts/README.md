# Helm packages

Charts package platform components with resource reservations, placement, network
boundaries and explicit external dependencies. Environment overlays and pinned
upstream dependencies live under [`deploy/`](../deploy/README.md).

| Chart | Purpose |
| --- | --- |
| [`klip`](klip/README.md) | Management API, workers, web, migrations and runner configuration |
| [`klip-database`](klip-database/README.md) | CloudNativePG-managed PostgreSQL, storage and backup configuration |
| [`klip-postgresql`](klip-postgresql/README.md) | Simpler PostgreSQL packaging for local/integration environments |
| [`klip-model`](klip-model/README.md) | Pinned vLLM model Pod, placement and networking |
| `klip-host-executor` | Separate restricted host-operation service and network/RBAC boundary |
| `klip-storage` | Shared artifact volume claims |
| `klip-workload-identity` | Scoped workload credential injection |
| `klip-image-pull` | Image credential audience/configuration |
| [`klip-observability`](klip-observability/README.md) | Telemetry network boundaries and support resources |
| `klip-workload-stub` | CPU-only inference stub for integration verification |

Rendering a chart checks configuration; it does not prove cluster readiness,
storage durability, GPU capacity or serving availability. Follow the
[deployment guide](../docs/development/management-deployment.md) and the component
runbooks before applying changes.
