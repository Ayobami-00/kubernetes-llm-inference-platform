# AWS access

Use account `127811062476`, London `eu-west-2`. The existing administrator IAM user
`kubernetes-llm-inference-platform` and its original local access-key profile remain
unchanged. The user has a registered passkey; do not share credentials, browser
sign-in URLs or MFA codes in chat or Git.

| Local profile | Purpose |
| --- | --- |
| `kubernetes-llm-console` | AWS CLI browser sign-in with the IAM user's passkey |
| `kubernetes-llm-bootstrap` | Credential-process bridge to that administrator session |
| `kubernetes-llm-foundation-session` | MFA-protected foundation role assumption |
| `kubernetes-llm-foundation` | Credential-process bridge for Terraform/SSM |
| `kubernetes-llm-dns-session` | MFA-protected DNS role assumption |
| `kubernetes-llm-dns` | Credential-process bridge for the independent DNS root |

```bash
make infra-profiles
aws login --profile kubernetes-llm-console
aws sts get-caller-identity --profile kubernetes-llm-foundation
```

Profile setup is append-only and stops if an existing project entry differs.
Credentials stay in AWS CLI's private local cache. The CLI bridge allows Terraform
to use console-login credentials without weakening MFA trust. A passkey's U2F ARN
is not an STS TOTP `mfa_serial`. Refresh browser sign-in if needed; tooling never
falls back to the original administrator key profile for a deployment operation.

Both deployment roles use IAM path `/kubernetes-llm-inference-platform/`, exact
source-user trust and positive `aws:MultiFactorAuthPresent`, with one-hour sessions:
`kubernetes-llm-foundation-deployer` and `kubernetes-llm-inference-dns-deployer`.
Only bootstrap uses the administrator identity. Normal foundation and DNS operations
use their respective roles; the management application receives neither.

## Ownership transition

The earlier access-bootstrap step created the foundation role, four attached
managed policies (`network`, `compute`, `api`, `identity-state`) and the separate
`kubernetes-llm-control-plane-boundary` policy. It verified MFA and role assumption
without creating compute/network/state resources.

The Terraform bootstrap root now includes explicit imports for all these existing
objects and attachments. On authorized apply it adopts them, adds the scoped SSM
operations policy and creates the separate DNS role/policy. It preserves the
source user's administrative access and MFA trust. The legacy CLI mutator refuses
`--apply` once the role is tagged `ManagedBy=terraform-state-bootstrap`; all later
changes go through Terraform. Adoption is not complete until that apply and remote
state verification succeed.

`scripts/infrastructure/aws_access.py` remains the canonical policy builder. Its
nine generated JSON documents are checked into the bootstrap root. Use
`make infra-policies` after an intentional builder change; `make infra-check`
rejects generation drift. Run the read-only cloud validation separately:

```bash
.venv/bin/python scripts/infrastructure/validate_access.py
```

This validates policies with Access Analyzer and checks positive/negative IAM
simulations, saving private findings and decisions. Simulations are evidence for
supplied IAM context, not proof that a complete AWS deployment will succeed.

## Permission boundaries

- EC2 writes require the project ownership tag and London region, including
  security-group-rule creation tags. Public keys have `klip-` names. The foundation
  role cannot remove ownership tags, create credentials or modify itself.
- Node IAM creation is restricted to `klip-control-plane`, requires the prepared
  permissions boundary, permits only SSM core attachment and allows PassRole only
  to EC2. No application AWS privileges are attached to nodes.
- SSM shell/SSH operations target project-tagged instances and approved AWS
  documents. These are privileged host operations; keep the operator identity
  separate from application credentials.
- Foundation state access covers its own key and lock. DNS state access covers
  only the DNS key/lock. Neither deployment role can delete its state data or
  administer the state bucket. Listing the bucket can reveal key names.
- Foundation has no Route53 writes. DNS record writes are restricted to the exact
  inference A name in the existing zone. Route53 health checks lack IAM tag
  conditions; see the explicit account-wide health-check limit in the DNS runbook.
- No Lambda VM lifecycle privileges, EKS resources or management application AWS
  access are introduced. Access/IAM deletion remains a separate administrator task.

The private API uses the NLB's AWS DNS name with kubeadm CA trust, not public ACM.
Replacing that NLB requires a deliberate endpoint/certificate migration. The
registered domain is for direct Lambda inference origins; zone registration and
delegation are retained account assets. Contact email verification remains an
independent registrar requirement.

References: [AWS MFA and CLI login](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_mfa.html),
[credential process](https://docs.aws.amazon.com/cli/latest/reference/configure/export-credentials.html),
[Route53 authorization](https://docs.aws.amazon.com/service-authorization/latest/reference/list_route53.html).
