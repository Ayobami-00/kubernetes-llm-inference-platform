# Retained platform storage

The `storage` root owns regional encrypted EFS, one static non-root artifact access
point, a private versioned PostgreSQL archive bucket and exact workload IAM roles.
It deliberately has no VPC dependency. The separate `storage-access` root owns
three EFS mount targets, their security group and a single CPU-worker NFS egress
rule. Destroy connectivity before the VPC; retain the filesystem, access point,
archive bucket, identity provider and their Terraform states during rebuilds.

Both roots use the existing **operator-only bootstrap identity**, checked against
this account's exact configured administrator. Neither changes the foundation/DNS
deployer policies nor grants these permissions to management, nodes or runner Pods.
Use the ordinary saved-plan review workflow:

```sh
make storage-plan
make storage-apply PLAN_SHA=<reviewed-plan-sha256>
make storage-access-plan
make storage-access-apply PLAN_SHA=<reviewed-plan-sha256>
```

`make storage-verify` compares actual encryption, retention and workload policies
with applied state and checks CPU-to-local-mount-target connectivity. It distinguishes
those checks from mounts, archiving and restore, which require subsequent acceptance.

Normal apply rejects deletes/replacements, account mismatch and source/plan drift.
Storage lifecycle guards also prevent accidental destruction. A complete purge
requires a separate procedure; it is not the retained-state acceptance path.

## Access and renewal

The EFS **node CSI service account** `kube-system:efs-csi-node-sa` alone receives the
artifact role. Mount using the static access point with `tls,iam`; the filesystem
policy denies cleartext, other access-point paths and root access. The access point
forces UID/GID 10001 and directory mode 0770. Worker security groups alone can reach
TCP 2049. Control-plane and ephemeral GPU groups receive no mount access. Management
and runner containers use the mounted filesystem without receiving AWS credentials.
The shared artifact tree is a trusted platform boundary, not per-customer isolation.
High-frequency request logs/exports must use local scratch and durable checkpoints,
not synchronous writes per request on EFS.

PostgreSQL runs in `platform-data`; `klip-db` and the explicitly reserved
`klip-db-restore` service accounts can assume the database archive role. The role can
read/write/delete current archive objects only under `postgresql/` and list only that
prefix. It cannot delete historical object versions. The role trust requires this
cluster's OIDC provider and the STS audience, with no wildcard subjects.

Set the database chart's `backup.authentication=webIdentity`, `backup.roleArn` and
empty `backup.credentialsSecret` after the supported projected-token injection path
is deployed and verified. It uses `inheritFromIAMRole`, not copied temporary keys.
The backward-compatible Secret mode is available for controlled tests; it is not a
renewal solution. Validate actual backup/restore and SDK renewal before relying on it.

The database NetworkPolicy combines the management namespace **and** Pod selectors
in one peer. Set `networkPolicy.managementNamespace=klip` when installing the database
in `platform-data`. Transfer only the public database CA certificate to management;
never distribute its private CA key.

## Retention and recovery

S3 enforces TLS, account ownership, AES256 encryption, versioning and public-access
blocking. Barman owns the **30-day recovery window** and current WAL/base-backup
expiry; the bucket must not independently expire current WAL by age. Deleted or
replaced versions remain recoverable for 45 days, subject to AWS lifecycle execution.
Abandoned multipart uploads expire after seven days. Backup retention is not verified
until actual archiving and restoration succeed.

EFS has no automatic file expiry. Artifacts/history remain retained until a separately
authorized application retention process removes them. AWS's automatic EFS backup
plan is enabled (daily / 35 days); verify successful recovery points and restore before
claiming recoverability. The regional filesystem survives mount-target and cluster
replacement. Independent data/WAL PostgreSQL claims will use encrypted topology-aware
EBS with Retain and WaitForFirstConsumer; CSI deployment remains a separate prerequisite.

## Cost envelope

An AWS Price List query on 3 November 2026 returned London prices of $0.33/GB-month
for EFS Standard, $0.03/GB read and $0.07/GB write for Elastic Throughput,
$0.06/GB-month for EFS warm backups, $0.024/GB-month for the first S3 Standard tier
and $0.0928/GiB-month for gp3 storage. There is no reserved EFS throughput charge.

A planning example of 20 GB EFS, 30 GB charged backup storage, 100 GB S3 archives,
20 GB EFS reads, 50 GB writes and the initial 180 GiB database data/WAL allocation
is about **$31.60/month** before S3 API requests, transfer, restore charges, tax and
telemetry storage. This is an explicit usage assumption, not a cap or capacity
reservation. The database disks are charged only once created by CSI. Replication
and cross-AZ traffic are additional. Watch growth and provision capacity alerts.
Raw dated quotes belong in ignored operational evidence, not Git.

Sources: [EFS IAM authorization](https://docs.aws.amazon.com/efs/latest/ug/iam-access-control-nfs-efs.html),
[access points](https://docs.aws.amazon.com/efs/latest/ug/efs-access-points.html),
[automatic backup plan](https://docs.aws.amazon.com/efs/latest/ug/awsbackup.html),
and [CSI mount identity](https://github.com/kubernetes-sigs/aws-efs-csi-driver/blob/master/docs/parameters.md).
