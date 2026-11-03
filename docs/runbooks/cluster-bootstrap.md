# Host preparation and cluster rollout

## Review first

Work only on feat/infrastructure. Refresh local tools with `make infra-tools` and
`make cluster-tools`, then run `make infra-check`. AWS sessions use the existing
MFA-protected profiles. If expired, run `aws login --profile kubernetes-llm-console`.
cluster-tools installs a pinned project-local Session Manager plugin; wrapped commands add
it to PATH. Public AWS SSH stays closed.

Before live rollout, review saved Terraform plans and the exact source digest,
[host changes and costs](../decisions/0003-cluster-rollout-review.md). This slice's
new AWS resources and persistent host changes require that rollout authorization.
Planning and IAM simulations do not constitute an apply.

```bash
make infra-export
make cluster-preflight
uv run --locked --group infrastructure python scripts/infrastructure/manage.py plan --root bootstrap
uv run --locked --group infrastructure python scripts/infrastructure/manage.py plan --root foundation
uv run --locked --group infrastructure python scripts/infrastructure/manage.py plan --root backups --preview
uv run --locked --group infrastructure python scripts/infrastructure/manage.py plan --root probe --preview
```

New-root previews use the existing privileged bootstrap identity with a local,
non-applicable backend because the foundation role does not yet have their state
permissions. After approval and the IAM apply, recreate real S3-backed plans. Never
apply previews or replace the original foundation state with preview state.

## Ordered rollout

1. Apply the reviewed bootstrap IAM plan using manage.py apply --root bootstrap
   --approve-plan SHA. No source-user or deployment-role trust changes are intended.
2. Create/review the real backups plan and apply it. Apply the real foundation plan
   to attach the bounded upload policy. Neither operation replaces CPU hosts/disks.
3. Initialize the local backup identity with cluster.py key-init if absent. Retain
   an independently stored copy outside Git/operator state, then run cluster.py
   key-verify-copy --copy /absolute/copied/key --confirm-independent-custody. Do not paste keys into chat or jobs.
4. Run `make cluster-prepare`, then `make cluster-mesh`. Verify peers before
   `make cluster-bootstrap`. Control planes initialize/join one at a time.
5. Run `make cluster-deploy`. CRDs install explicitly, Cilium becomes ready, known
   kubelet serving CSRs are checked/approved, then metrics-server installs.
6. Run `make cluster-backup` to schedule daily backups and obtain initial recovery
   bundles. Re-running bootstrap checks existing members; it does not reset them.
7. Create/review/apply the real probe plan. Run `make cluster-acceptance`. It uses
   real HTTPS/API writes, never the foundation's prebootstrap HTTP listeners.
8. Review private reports, cleanup and all follow-up Terraform plans. Leave all
   CPU hosts running. Commit results on feat/infrastructure; never push to main.

The test host's four-hour absolute shutdown timer survives reboot and terminates
compute. Terraform cleanup also removes its EIP, ENI, VPC and IAM resources; expiry
alone does not remove those resources. The acceptance finalizer restarts the tested CPU gateway,
revokes probe credentials/peers and destroys the probe after UID-guarded removal.
Failures and unfinished cleanup remain explicit in cluster/acceptance.json.

## Operator access and repeat runs

Generated inventory and strict known_hosts pins remain private. Refresh inventory
on instance replacement; do not disable host-key checking. cluster.py uses a local
SSM port-forward to16443, with tls-server-name set to the NLB hostname. It never
uses insecure-skip-tls-verify. B's admin kubeconfig is not a Stream C credential.

Use `cluster.py approve-csrs` for serving-certificate renewal after examining the
pending requests. Approval checks authenticated node identity, provider ID, CSR
signature, subject, SANs and permitted usages. Unknown or conflicting requests fail
closed. Do not approve every CSR with a blanket cluster-wide approver.

A healthy rerun must preserve CA, etcd member IDs and node UIDs. A missing mount,
partial bootstrap, lost WireGuard key, unexpected etcd membership or mismatched
identity stops automation. Diagnose and use the recovery runbook; never run
kubeadm reset, mkfs or Terraform replacement to bypass these guards.

Full export follows live Pod/Service/DNS, WireGuard, etcd, MTU, packet-path and Hubble
checks. It emits real CA/node references under reports/infrastructure-inventory.json.
Ship the public CA alongside inventory to C and map its file reference in C's
artifact resolver. Keep admin kubeconfigs and CA private keys with B.
