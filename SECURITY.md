# Security and deployment scope

This is an experimental inference platform with recorded live acceptance and
production-oriented controls. It is not a production certification or a claim
that every infrastructure/serving failure has been tested.

## Report a security issue

Use the repository's private security-advisory reporting channel if enabled, or
contact the maintainer privately. Do not post an exploit, credential, private
artifact download, account inventory or unredacted diagnostic bundle in a public
issue. Public issue discussion can follow once sensitive details are removed.

## Deployment boundaries

- Keep inference and telemetry private unless a separate exposure/TLS review is complete.
- Supply credentials using runtime Secret references. Never commit `.secrets`,
  `.env`, kubeconfigs, private keys, Terraform plans/state or recovery bundles.
- Keep host-operation/SSH custody separate from the management application and
  benchmark Jobs. Review narrowly scoped RBAC and provider ownership before enabling mutations.
- Validate deployed versions, artifacts, capacity, leases and readiness; authored
  configurations or accepted policies alone do not prove execution readiness.
- Treat image-risk reviews as scoped and expiring. An expired or mismatched
  review blocks admission; it is not permission to ignore a new finding.
- Protect externally retained capacity from automatic expiry, replacement or deletion.

The recorded A10 experiment used time-limited model and endpoint-picker reviews
with retained findings. Their exact conditions are in `deploy/retained-a10-*-risk-review.json`
and the [retained A10 runbook](docs/runbooks/retained-a10-speculation.md). Historical
results remain evidence of that tested environment after a review expires; expiry
does not authorize another live deployment or benchmark.

## Public source and private evidence

Public figures use aggregate measurements and source/report checksums. Prompts,
correction references, responses, request-level records and private recovery data
remain in ignored storage. Infrastructure account IDs, domains and immutable
image/model hashes can identify the reference deployment; they are not credentials
and must be replaced deliberately when configuring another environment.

Before changing repository visibility, scan the reachable Git history as well as
the current files. Rotate any real exposed secret; deleting it from a later
commit does not revoke it. Exact reviewed false-positive fingerprints may be
documented, but do not broadly exclude application or infrastructure directories.

For the source-history check:

```bash
gitleaks git --redact=100 --log-opts HEAD
```

`.gitleaksignore` records exact reviewed historical fingerprints for public
model/tokenizer revisions, synthetic idempotency labels and an ACM algorithm
name. It does not exempt whole directories. A clean scan is one check, not proof
that all sensitive information or deployment risks have been eliminated.
