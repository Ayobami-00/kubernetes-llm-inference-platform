# KLIP management chart

C-owned API, durable workers, compiled web application and migration hook. Install PostgreSQL first:
use `klip-postgresql` for the local proof, or `klip-database` for the HA deployment below.
Supply existing authentication/database Secrets, an exact
public origin and a shared RWX artifact claim. Defaults keep every real mutation adapter unconfigured.

See [deployment instructions](../../docs/development/management-deployment.md),
[integration protocol](../../docs/development/management-integrations.md), and
[recovery procedures](../../docs/runbooks/management-recovery.md).

Use release-digest backend/web images. Production runner images must also be digest-pinned. Ingress
is opt-in and requires an existing TLS secret. B owns installing this static release; C owns the chart.

## HA placement and database TLS

Apply `deploy/environments/lab/management.yaml` alongside private deployment values to place
two replicas of each application component on persistent platform workers. This removes the
default control-plane selector, requires separate hosts, spreads across labelled zones and
creates disruption budgets. Rolling updates replace one replica without a surge, allowing
updates with strict anti-affinity when all eligible hosts are occupied.

Production HA requires digest-pinned application images, `database.requireTLS: true` and an
existing CA Secret. The connection URL must use the CA-covered service hostname; an explicit
TLS downgrade is rejected. Migrations receive the same CA and verified-TLS settings as the
API and worker. Existing local deployment behavior remains available without the HA overlay.

For PostgreSQL HA, use the separate `charts/klip-database` resource definitions after a verified
migration from the existing single-primary deployment. Do not install competing database
owners against the same storage. Live failover, identity, storage and restore verification are
tracked in the end-to-end delivery ledger.
