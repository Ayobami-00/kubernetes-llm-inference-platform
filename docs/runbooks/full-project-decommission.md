# Full AWS decommission — 5 November 2026

The reference experiment environment was retired at the owner's request after
the A10 comparison. Management, Grafana and the cluster endpoints are offline.
The checked-in aggregate results and plots remain historical evidence; local
development and CPU-only synthetic execution remain available.

This is a dated operational record, not an unattended destroy command. The
[foundation teardown](foundation-teardown.md) intentionally retains infrastructure
that this separately authorized full retirement removed.

## Scope and retained ownership

The retirement covered project-owned AWS resources. It excluded unrelated account
resources and preserved the operator's administrator login identity. The Lambda
A10 was not stopped, restarted or deleted; its owner handles its deletion.

The `klip-lab.click` domain registration remains owned by the user with **auto-renew
disabled**. Its project hosted zone was removed, so the previous nameservers no
longer serve the application. Retaining a registration does not keep services online.

## Removal and verification

| Layer | Decommission outcome |
| --- | --- |
| Compute and network | Three control-plane and three CPU-worker instances terminated; project VPC, API NLB, management ALB, network interfaces and Elastic IPs removed |
| Block storage | Root disks, three standalone etcd disks and 20 retained Kubernetes PV disks removed; no EBS volumes or owned snapshots remained in the deployment region |
| Shared filesystems | Both EFS filesystems/access points and mount targets removed; their two automatic-backup recovery points deleted |
| Durable objects | All project S3 buckets, including every object version/delete marker in data, telemetry, backup, identity and Terraform-state buckets, removed |
| Images and identity | Project ECR repositories/images, workload and CI OIDC providers, dedicated IAM roles/policies and CloudFront issuer resources removed |
| Frontend and alerts | Project certificates, WAF, DNS records/zone, health checks, alarms and alert topic/subscription removed |
| Retired KMS key | `PendingDeletion`; AWS reports permanent deletion scheduled for **5 December 2026** |

Full deletion plans and state exports were saved privately before applying exact
plan checksums. Retention guards were changed only in an ignored decommission
copy of Terraform; source retention protections remain enabled. Terraform's retained
CloudFront distribution was deleted explicitly after ownership, disabled state and
deployment status were checked. Kubernetes-created disks and EFS backup points
were reconciled against the original PV/filesystem inventory rather than inferred
from Terraform alone.

The automatic EFS backup vault's deletion-deny policy was adjusted only for the
two identified project recovery points, then restored. The empty AWS-managed vault
remains; it contains no recovery points. The Terraform backend was removed last,
using a preserved local bootstrap state so teardown could finish after S3 deletion.

Final checks used service inventories in the deployment regions and a project-tag
scan across all 17 enabled regions. Tagging results can retain stale deleted EC2
identities; live EC2 service responses were used to distinguish those from resources
still present. Unrelated repositories, log groups and account defaults were left alone.

## Recovery custody

Private evidence is under the operator checkout's ignored
`artifacts/teardown-20261105/` directory. It is intentionally absent from Git.
The `recovery/` subdirectory contains age-encrypted archives of:

- Shared execution and experiment artifacts, including the paired A10 report.
- Native management/Grafana database dumps and database globals.
- Restricted host-executor state.
- A fresh etcd snapshot, PKI and Secret-encryption recovery material.

Encrypted exports were decrypted and hash-verified locally. The etcd snapshot
also passed version-compatible status inspection and an isolated offline restore.
A running read probe and full cluster disaster recovery were **not** established
by that check. The temporary plaintext snapshot was removed after verification;
the encrypted archive remains. Keep the existing age identity and its independent
copy separate from these archives. Loss of that identity prevents decryption.

Historical Prometheus/Loki/Tempo object stores and cloud backup versions were
deleted rather than retained as an online service. The archived experiment
diagnostics and checked-in aggregates are the available result evidence.

## Before rebuilding

Rebuilding is a new deployment and still requires a separately measured destructive
rebuild acceptance. Do not reuse old observations as current readiness evidence.

1. Preserve this recovery directory and the operator's private state home. Use a
   fresh operational state directory, or explicitly reconcile the final empty local
   bootstrap state; old bootstrap recovery copies still describe deleted resources.
2. Review account, region, capacity and DNS inputs. Create a new hosted zone, update
   the registrar's nameservers and replace the deleted hosted-zone ID in deployment
   configuration. Auto-renew remains off unless the owner changes that decision.
3. Re-establish the initial access-bootstrap prerequisites before the normal adoption
   workflow. The current wrapper expects existing foundation access objects; it does
   not automatically recreate that pre-Terraform seed from an empty account.
4. Recreate state, infrastructure, workload identity, storage and registry through
   reviewed plans. Rebuild/publish images and collect fresh immutable receipts before
   enabling workflows that target the removed ECR repositories and CI role.
5. Bootstrap/configure Kubernetes and deploy database, management, observability and
   serving releases in dependency order using the existing runbooks. Restore data
   only through the corresponding reviewed recovery procedures.
6. Supply new inventory, credentials, node/Job identities and readiness evidence.
   Re-run authentication, backup/restore, telemetry and workload checks before
   launching another paired experiment.

Recreating infrastructure alone does not prove that old data, credentials or
external GPU capacity have been restored. No rebuild or GPU allocation was performed
as part of this retirement.
