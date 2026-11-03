# Replicated management PostgreSQL

The lab database is `platform-data/klip-db`: three PostgreSQL instances on distinct
protected CPU workers and separate encrypted gp3 data/WAL volumes. It uses
PostgreSQL 18.6 (standard Debian Trixie image), CloudNativePG 1.30.1 / chart 0.29.1,
and Barman Cloud Plugin 0.15.1 / chart 0.8.1. Chart checksums and Linux AMD64 image
digests are in `deploy/platform-services.lock.json`. The two operators share
`cnpg-system`; CloudNativePG watches only `platform-data`.

One required synchronous standby and failover quorum favor acknowledged-write
durability. Two unavailable database hosts may block writes. Zonal volumes do not
move between AZs; another replica serves after failover. See the initial
[availability objectives](../architecture/platform-availability.md). Replica
counts and Ready conditions do not prove application failover or backup recovery.

The CNPG controller has two replicas. The pinned Barman manager intentionally has
one recoverable replica: its leader-gated gRPC server and health-only readiness
probe do not support advertising a standby as a healthy plugin endpoint. Database
Pods each run their own bounded Barman sidecar. Measure manager-loss recovery before
claiming this dependency meets the platform's availability objective.

## First installation

Prerequisites are the verified protected CPU workers, OIDC/token injection,
retained S3 archive allocation and topology-aware `klip-gp3` storage class. The
database does not use host-role credentials or an AWS access-key Secret.

```sh
make platform-services-fetch
make platform-service-deploy SERVICE=cloudnative-pg
make platform-service-deploy SERVICE=plugin-barman-cloud
make database-prepare
# Inspect the private initialization receipt, frozen values and resource envelope.
make database-initialize PLAN_SHA=<exact-approval-sha256>
make database-verify
```

Initialization is exclusively for a new, empty archive identity. Any data in the
archive blocks it; existing database resources are never silently reinitialized.
The frozen receipt binds the namespace, storage class, source and values hashes.
Intent is persisted before installation, and an interrupted invocation reconciles
the same named cluster. Helm wait alone does not prove PostgreSQL is ready.

The application password is generated once into the private operator credential
store and streamed into a basic-auth Kubernetes Secret. A later invocation checks
both copies agree; it does not replace live credentials. Never display this file,
put its contents in Helm values or pass passwords as command arguments. The local
copy alone is insufficient recovery custody. Use the already verified independent
age identity to encrypt, upload and download/decrypt-check a versioned recovery object:

```sh
make database-backup-secret
make database-verify-secret
# During rebuild, recover the private custody receipt first, then restore only
# when the local credential file is absent; a conflicting existing file is rejected.
make database-restore-secret
```

The encrypted payload is outside the workload-readable `postgresql/` object prefix.
The private custody receipt binds bucket, key, version, ciphertext checksum and
recovery-key identity. Upload intent precedes the S3 write; ambiguous uploads are
reconciled by that key/checksum. The secret never enters command arguments or logs.

The main allocation is 180 GiB: three 40-GiB data volumes and three 20-GiB WAL
volumes. At the reviewed London gp3 rate of $0.0928/GiB-month this is about
$16.70/month, excluding snapshots, requests and transfer. External backups use a
private versioned S3 bucket and Barman's 30-day recovery window. There is no blanket
current-object age expiry that could remove required WAL/base backups.

## Archive and backup acceptance

Inspect `ContinuousArchiving`, `pg_stat_archiver` and the actual object-store
backup status. Preserve historical failure counters after a repair. The workload
role allows `HeadBucket` through bucket-level `ListBucket` on the dedicated backup
bucket; object writes/deletes remain restricted to `postgresql/`. This is required
by Barman's archive safety check. The database has a 60-second archive timeout,
not a guarantee that every commit reaches object storage within 60 seconds.

```sh
# Choose one stable operation identity; repeated calls reuse its existing backup.
make database-backup OPERATION=<12-lowercase-hex-characters>
```

This creates a synthetic base marker, completes a real plugin base backup, then
commits another marker and archives a named restore point. It retains backup UID,
backup ID, LSN/WAL bounds and archive observations. It never overwrites an existing
backup with another resource of the same name. A failed operation requires
inspection; it does not automatically create another identity.

## Isolated restore acceptance

```sh
make database-restore-prepare OPERATION=<same-operation>
# Review the new archive destination, pinned source backup and temporary capacity.
make database-restore OPERATION=<same-operation> PLAN_SHA=<exact-approval-sha256>
make database-verify-restore OPERATION=<same-operation>
```

The reserved recovery identity is `platform-data/klip-db-restore`, whose exact
service account is already allowed by the scoped backup role. Only one such test
cluster may exist. Recovery reads the pinned source backup and named post-backup
WAL point. Its own archive output goes to `postgresql/restore-<operation>/`; source
backup retention is never changed. Recovery cannot fall back to `initdb`.

The test uses another three instances / 180 GiB temporarily, then checks promoted
read/write state and both markers on distinct hosts. Private evidence lives under
`cluster/database/recovery/<operation>/` in the operator state store: backup receipt,
reviewed restore plan, rendered manifests and verification. Timestamps distinguish
backup completion, WAL archival, restore start and verification. An isolated
synthetic marker test does not establish management-history migration, client
reconnection, RPO under archive failure or a full environment rebuild.

Retain the source database and backups. Before removing a test restore, record its
Cluster UID, PVC/PV UIDs and underlying EBS IDs. Use exact UID-conditional deletion;
the storage class retains volumes, so removing a namespace/release alone neither
deletes the disks nor proves cleanup. `make database-cleanup-restore OPERATION=...`
is limited to a verified synthetic marker restore with no application tables. It
records all six claim/PV/disk identities, deletes only the reserved Cluster using
an API-side UID precondition, waits for claim removal/detach, then switches only
those verified Released PVs to Delete with atomic UID/claim/disk tests. It confirms
the underlying AWS disks are absent. Source backups and archive metadata remain
retained. Do not patch production volumes to Delete.

## Application cutover

Keep the original local management database until a verified dump, restoration,
row/history checks and rollback procedure exist. Connect to
`klip-db-rw.platform-data.svc` with TLS hostname/CA verification. Copy only public
`ca.crt` into management's CA Secret, never the database CA private key. Migrations,
API and workers must use the same role/database and CA. No external superuser
Secret is enabled. Grafana requires a separate database and role.

Before enabling lifecycle mutations, verify authenticated management requests,
durable operations and artifact access against the restored/migrated data. Test
primary loss and actual application reconnection separately, recording committed
markers and observed recovery time.

Sources: [pinned Barman usage](https://cloudnative-pg.io/plugin-barman-cloud/docs/0.15.0/usage/),
[CloudNativePG recovery schema](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.1/api/v1/cluster_types.go),
and [S3 HeadBucket permissions](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadBucket.html).
