# Management PostgreSQL HA

This chart declares a three-instance CloudNativePG cluster with one required synchronous
standby and failover quorum, separate data/WAL volumes, strict host anti-affinity and zone
spreading. It refuses a floating image or fewer than three instances. Losing both standbys
can block writes: the selected policy prioritizes durability over write availability.

CloudNativePG 1.30.1 (chart 0.29.1), Barman Cloud Plugin 0.15.1 (chart 0.8.1), and
PostgreSQL 18.6 standard Trixie are digest/checksum pinned and deployed on the live
Kubernetes 1.36 lab cluster. Continuous WAL archiving, an external base backup and
an isolated three-instance restore have passed. Both a base-backup marker and a
later WAL marker were recovered. Management history is migrated; application deployment
and actual failover remain pending. This chart does not install either operator. See the
[operational runbook](../../docs/runbooks/management-postgresql.md).

## Prerequisites

- Three persistent worker hosts labelled `klip.dev.node-restriction.kubernetes.io/platform=true`, with observed
  `topology.kubernetes.io/zone` labels and enough resources after one host loss.
- Retained, encrypted, topology-aware RWO storage for independent data and WAL claims.
- Compatible CloudNativePG and Barman operators, including their certificate prerequisites.
- An existing application Secret of type `kubernetes.io/basic-auth`, with `username`
  matching `owner` and a `password`. Never put its values in Helm arguments or Git.
- A dedicated archive prefix and scoped AWS identity. Production uses
  `backup.authentication=webIdentity`, `backup.roleArn` and an empty credentialsSecret,
  with verified projected-token injection and SDK renewal. The chart sets the exact
  service-account role annotation and `inheritFromIAMRole` for Barman. The compatible
  Secret mode expects `access-key-id`, `secret-access-key`, `session-token` and `region`;
  a one-time expiring credential is insufficient for production.
- An isolated database namespace (`platform-data` for the live environment). Set
  `networkPolicy.managementNamespace=klip`; its namespace and Pod selectors are combined.
  See the [retained storage runbook](../../docs/runbooks/platform-storage.md).
- The live application connects to `<release>-rw.<namespace>.svc`, verifies TLS against
  `<release>-ca`, and uses the matching database role. The management HA overlay mounts
  that CA for migrations, API and workers.

Provide verified image/Secret/storage/archive identifiers in a private values file. Install
the database release before management and explicitly wait for the Cluster `Ready`
condition; Helm's ordinary wait does not establish PostgreSQL readiness. Enable PodMonitor
only after the monitoring CRD exists. The NetworkPolicy permits application/migration
clients, replication, operator management and monitoring separately.

## Restore boundary

`bootstrap.mode=initdb` initializes a new database. `bootstrap.mode=recovery` selects
physical recovery through the plugin and requires the original archive path/server name.
Recovery never falls back to an empty database. It explicitly retains the application
database, owner and credential Secret; `bootstrap.recovery.target` can bind recovery
to a backup ID and named restore point. Use a new cluster identity or archive
prefix; the chart rejects writing back to the exact source identity. The source ObjectStore
has no retention policy, so the recovery definition cannot expire its source backups.

For a rebuild, stage recovery first, verify historical records and application credentials,
then cut over the application connection. Preserve the original backup and archive until
the recovery gate passes. The existing single-instance chart remains available for local
tests; replacing it in a running environment requires a verified migration procedure.

Cluster and ObjectStore resources carry Helm keep annotations. These annotations do not
replace PV reclaim policies, namespace-deletion safeguards or independently retained S3
backups. Planned primary upgrades are supervised; emergency failover remains operator-managed.

## Verification

```sh
make platform-schema-fetch  # fetch checksum-pinned schemas and validate both bootstrap paths
make platform-check         # repeat offline using that cache
uv run --locked --all-packages pytest tests/infrastructure/test_platform_deployment.py
```

Render checks use explicitly synthetic image/Secret/bucket identifiers and cannot deploy.
Structural schema validation also rejects unknown fields that Kubernetes might prune. It
does not execute Kubernetes CEL, prove runtime compatibility or certify recovery behavior.

Sources: [CloudNativePG support matrix](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.1/docs/src/supported_releases.md),
[replication policy](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.1/docs/src/replication.md),
and [Barman Cloud Plugin](https://cloudnative-pg.io/plugin-barman-cloud/docs/intro/).
