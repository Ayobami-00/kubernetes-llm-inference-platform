# AWS foundation provisioning review

Prepared 3 November 2026 on `feat/infrastructure`, starting from `73ef9ec`.
This records the pre-provisioning checkpoint; it is not a live acceptance report.

## Plans and ownership

Read-only discovery confirmed the pinned Canonical AMI is available in London,
all three selected AZs are available and the existing public zone is readable.
No Terraform apply or temporary host smoke command has been executed.

| Root | Review result |
| --- | --- |
| Bootstrap | 10 existing IAM imports, 11 additions, 6 in-place updates, no deletions |
| Foundation preview | 61 additions, no replacements/deletions |
| Empty-origin DNS preview | No record or health-check changes |

Bootstrap imports the role, five policies and four existing attachments. Five of
the six updates change ownership tags only; the network policy also adds tagged
security-group-rule creation support. New resources comprise the S3 bucket and
its five controls, two managed policies, the DNS role and two policy attachments.
Existing MFA trust and source-user access are preserved.

The foundation creates three m7i.large hosts, three 40-GiB root disks, three separate
20-GiB etcd disks, three ENIs/EIPs, six subnets, the VPC/IGW/routes/security groups,
an SSM-only node identity and one internal TCP NLB with three private-IP targets.
Termination/deletion protection is enabled. No public TCP22/6443 ingress, NAT
gateway, Kubernetes, WireGuard configuration, GPU VM or application is installed.

The separate DNS role can change only the intended inference A name. Its health
check operations have the documented Route53 IAM limitation: tag authorization is
unsupported, creation requires wildcard resource access and later operations
cover health-check ARNs account-wide. No checks are created with empty origins.

Private saved plans, JSON/text renderings and receipts are in
`~/.local/state/kubernetes-llm-inference-platform/plans/`. Plan identifiers:

```text
bootstrap:          af9f3ed192275e8b89c3bb04916b365b6061cd3257ccb00eebbd2247702c9b8f
foundation-preview: 1c9cad20c5752c8c264effcedbab85ba22e3839397c191e680edb94b0b66c83d
dns-preview:        4a6a3af504fceeef3a62f5d4da9a37ce6a6049f5fc0a2968bc2f8f6951b00309
```

Previews cannot be applied through the wrapper. After bootstrap state migration,
generate real S3-backed foundation/DNS plans under their scoped identities and
compare them with this scope. Changed configuration/inputs invalidate approval
receipts and require fresh plans. The current implementation digest is recorded
in each private receipt.

## Validated locally

- Terraform formatting, backend-free initialization and validation across all
  three roots and four modules; TFLint with the pinned AWS ruleset.
- Eleven credential-free Terraform mock tests, including private API settings,
  protected/encrypted compute, CIDR/tag failures and empty/weighted/invalid DNS.
- 235 Python tests, including all original 137 contract tests, IAM invariants,
  state recovery/conflicts, inventory truthfulness, invalid inputs, teardown guards
  and smoke-test recovery/cleanup failures.
- Ruff lint/format and all twelve generated-schema checks. Shared models/schemas
  remain unchanged. Provider locks cover macOS ARM64 and Linux AMD64.
- Read-only AWS Access Analyzer validation and IAM positive/negative simulations;
  detailed evidence stays in the private reports directory.

## Monthly cost and live scope

| Component | USD/month at 730 hours |
| --- | ---: |
| Three continuously running m7i.large instances | 255.24 |
| 180 GiB gp3 | 16.70 |
| NLB hourly base | 19.32 |
| Three public IPv4 addresses | 10.95 |
| Existing public hosted zone | 0.50 |
| **Baseline** | **302.71** |

Exclude tax, variable traffic, NLB capacity units, S3 state storage/requests and
future health checks. Domain registration is separately USD3/year at the observed
current price. Detailed unit prices and assumptions are in the provisioning runbook.

Authorized live work will apply bootstrap/migrate/verify state, provision the
foundation, apply empty DNS outputs, verify actual AWS resources/SSM, run temporary
expiring HTTP listeners on6443, stop and restart cp-03, check NLB convergence and
recovery, remove all listeners, export truthful observations and check for drift.
Leave all three hosts running. Unexpected replacements or broader scope require
review, not automatic permission widening or destructive workarounds.

Live EC2/storage/NLB/SSM/failure acceptance and a clean post-apply plan remain
pending authorization. Kubernetes API/etcd quorum, WireGuard, Cilium and inference
failover remain later milestones regardless of transport smoke-test success.
