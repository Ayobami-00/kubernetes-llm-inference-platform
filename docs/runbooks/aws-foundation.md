# Provisioning the AWS foundation

Use only `feat/infrastructure`. Preserve other worktrees and run from this worktree's
root. No command below authorizes itself to spend money: review the generated plans
and cost estimate before executing an apply. Never apply a preview plan directly
with Terraform; the operator wrapper deliberately rejects them.

## Tooling and identities

```bash
uv sync --locked --all-packages
make infra-tools
make infra-check
make infra-profiles
aws login --profile kubernetes-llm-console
```

Tools are downloaded into ignored `artifacts/tools/bin` and verified against
checked SHA-256 pins. System installations remain unchanged. Provider lock files
cover macOS ARM64 and Linux AMD64. `make infra-lock` refreshes platform checksums
without upgrading provider versions and also runs the complete check suite.

The bootstrap profile is `kubernetes-llm-bootstrap`, a credential-process bridge
to the browser-authenticated administrator IAM user. Normal deployment uses
`kubernetes-llm-foundation`; DNS uses `kubernetes-llm-dns`. Their source is the same
passkey-authenticated console identity, but the roles have separate scoped rights.
The original administrator user's access is preserved. No credentials are embedded
in Terraform or supplied to the management application. Expired sign-in requires
the operator to repeat `aws login`; tooling does not fall back to other profiles.

Private plans, recovery state, the dedicated local SSH key and observed inventories
are stored under `~/.local/state/kubernetes-llm-inference-platform/`, with private
directory/file modes. `KLIP_STATE_HOME` can relocate that directory; preserve it
across worktree changes. The `credential://klip/foundation-ssh` alias resolves through
its private `credentials.json`. Only the public key is imported into EC2.

## Plan before creating resources

```bash
make infra-bootstrap-plan
make infra-preview
make infra-dns-preview ORIGINS_FILE=infra/terraform/environments/inference-dns/origins.example.tfvars.json
```

Bootstrap's initial saved plan uses local state and imports the existing role and
five managed policies; it does not recreate or detach them. The additional SSM
operations and DNS policies/role are visible in that plan. Each plan produces a
private text rendering, machine-readable JSON, saved `.tfplan` and receipt. Review
resource changes, IAM documents, account, addresses, AMI, costs and imported IDs.
Preview plans for foundation/DNS use isolated empty local state and the bootstrap
identity for read-only discovery; they are marked unapplyable. They do not prove
the final deployment role has every required permission.

## Apply after authorization

```bash
make infra-bootstrap-apply PLAN_SHA=<reviewed-bootstrap-plan-sha256>
make infra-plan
make infra-apply PLAN_SHA=<reviewed-foundation-plan-sha256>
make infra-dns-plan ORIGINS_FILE=infra/terraform/environments/inference-dns/origins.example.tfvars.json
make infra-dns-apply PLAN_SHA=<reviewed-dns-plan-sha256>
```

Bootstrap apply automatically verifies and migrates its state to S3. Subsequent
roots use separate keys and identities. Real foundation/DNS plans must be generated
after bootstrap; preview plans cannot be reused. Apply checks the exact plan hash,
input hash, source hash, account, expected identity and plan root, and rejects
replays. Normal plans reject destruction/replacement; use a reviewed maintenance
or teardown procedure for those changes. Do not use `-auto-approve` against an
unreviewed plan or remove IAM conditions to conceal deployment failures.

If provider calls expose a missing permission, diagnose the precise action/resource,
change the canonical IAM builder, regenerate its documents, validate and replan
bootstrap. The foundation role cannot rewrite its own permissions. Once Terraform
adopts the access objects, the old standalone CLI access writer must not be used.

The live deployment exposed distinct EC2 authorization checks for existing parent
VPCs and newly tagged security-group-rule ARNs. The canonical policy now separates
parent ownership checks from request-tag checks on new resources. The pinned
provider also reads `DescribeAddressesAttribute`; that read is region-scoped.

If a provider creates an object but then fails only its refresh, Terraform may mark
it tainted. Do not replace it to bypass the failed read. Preserve a private state
backup, diagnose/fix the precise permission, verify the object's ID, ownership,
region and expected configuration directly in AWS, and then clear only the
reviewed creation-error taint with `terraform untaint <exact-resource-address>`.
The initial EIP recovery followed this procedure and retained all three allocations.
Generate a fresh plan afterward and reject unexpected replacement/destruction.

## Verify and leave running

```bash
make infra-verify
make infra-smoke
make infra-export
make infra-plan
```

`infra-verify` checks live AWS placement, disks, addresses, IMDS, security groups,
routing, NLB settings and SSM access, then emits truthful observations. Reports
are private and separate from Terraform allocations. The final plan should contain
no unexpected changes. Leave all three hosts running for the Ansible milestone.

`infra-smoke` is an explicit prebootstrap outage test: it refuses Kubernetes
configuration or an existing 6443 listener, starts bounded temporary HTTP services
through SSM, checks NLB traffic from all hosts, stops cp-03 and records convergence
errors, then checks requests against the surviving hosts. Its finalizer restarts
cp-03 and removes listeners; units independently expire after twenty minutes.
Review cleanup failures as failures, not successful acceptance. After cleanup the
NLB targets will be unhealthy until Kubernetes is installed; that is expected.
This proves AWS TCP transport, not Kubernetes API TLS, etcd quorum, WireGuard or
inference availability. Never reuse it against a bootstrapped cluster.

## Cost baseline

Rates checked through the AWS Price List API on 3 November 2026, assuming 730 hours:

| Component | Calculation | USD/month |
| --- | --- | ---: |
| Three on-demand m7i.large hosts | 3 × 730 × 0.11655 | 255.24 |
| 180 GiB encrypted gp3 | 180 × 0.0928 | 16.70 |
| Internal NLB | 730 × 0.02646 | 19.32 |
| Three public IPv4 addresses | 3 × 730 × 0.005 | 10.95 |
| Existing public hosted zone | 1 × 0.50 | 0.50 |
| **Baseline** | | **302.71** |

Tax, data transfer, NLB capacity units (USD 0.006/LCU-hour), S3 state storage/requests
and future gateway health checks are additional. The domain's current annual price
is USD 3, already separate from this monthly foundation estimate. Empty origins
create no gateway health-check charges. No Spot, reservations, Savings Plans, NAT
gateways or application databases are purchased by this slice.
