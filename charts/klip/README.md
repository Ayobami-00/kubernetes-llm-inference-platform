# KLIP management chart

C-owned API, durable workers, compiled web application and migration hook. Install PostgreSQL first
using the separate `klip-postgresql` chart. Supply existing authentication/database Secrets, an exact
public origin and a shared RWX artifact claim. Defaults keep every real mutation adapter unconfigured.

See [deployment instructions](../../docs/development/management-deployment.md),
[integration protocol](../../docs/development/management-integrations.md), and
[recovery procedures](../../docs/runbooks/management-recovery.md).

Use release-digest backend/web images. Production runner images must also be digest-pinned. Ingress
is opt-in and requires an existing TLS secret. B owns installing this static release; C owns the chart.
