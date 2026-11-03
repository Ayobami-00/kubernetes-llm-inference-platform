# Inference origin updates

The domain `klip-lab.click` and public zone `Z06158312NEW6IHHPH5XV` already exist.
This root reads the zone and manages only weighted A records for
`inference.klip-lab.click` and their health checks. Registration, delegation, NS and
SOA remain outside Terraform ownership. Complete the registrar's pending contact
email verification independently; successful delegation does not complete that step.

Initial configuration is an explicitly empty origin map. It creates no A records
or health checks. An AWS-owned NLB hostname is the private Kubernetes API endpoint;
it cannot serve as a custom DNS name for unrelated Lambda gateway addresses.

## Foundation-operated workflow

1. C supplies an origin-change request containing stable origin IDs and the public
   IPv4 addresses of gateways whose TLS certificates and application readiness
   have been independently verified. C allocates/terminates Lambda VMs; it receives
   no AWS credentials to perform this workflow.
2. An authorized foundation operator reviews the addresses, cost and serving
   readiness. Keep the explicit origin-map JSON outside Git if it contains private
   operational details. Keys must be stable kebab-case IDs, addresses must be
   globally routable IPv4 and unique, and weights must be integers 1–255.
3. Run `make infra-dns-plan ORIGINS_FILE=/absolute/path/origins.json`, review the
   saved plan and apply its exact hash with `make infra-dns-apply PLAN_SHA=...`.
   The DNS profile assumes the separate MFA-protected DNS role and has access only
   to its state key and the intended zone/name/type for record changes.
4. Observe authoritative DNS answers, health-check status, external HTTPS requests
   and application behavior before treating an origin as serving traffic. For
   removal, first withdraw the origin from DNS, allow caches/streams to drain and
   then let C terminate it. Existing streams never migrate to another origin.

```json
{
  "origins": {
    "worker-pool-a": {"ipv4": "<actual-public-IPv4>", "weight": 1}
  },
  "health_path": "/readyz"
}
```

The placeholder deliberately fails validation. Omitted weight defaults to one.
Omitting `--origins-file` is an error, preventing accidental withdrawal by default.
The normal apply wrapper refuses deletions/replacements: an intentional origin
withdrawal requires the explicit reviewed DNS change workflow described below.

For origin removal/replacement, review the saved Terraform plan text/JSON and use
`plan --root dns --origins-file /absolute/path/origins.json --allow-origin-removal`
through `scripts/infrastructure/manage.py`, which permits deletion of
inference records and health checks only. Never use foundation destroy or widen
the DNS role to unrelated record names. Keep the request and plan hash in the
operational change record.

## Health and security limits

Weighted records use TTL 30 seconds and individual HTTPS health checks on port443,
with Host/SNI `inference.klip-lab.click`, `/readyz` by default, a 30-second interval
and failure threshold three. DNS chooses an origin address; it is not a proxy or
llm-d endpoint selector. Health evaluation, authoritative changes, resolver caches
and client caches all introduce delay beyond the TTL.

Route 53 can return unhealthy records when all candidates are unhealthy. Its HTTPS
health checks do not validate certificates. Keep external TLS validation and real
application readiness as separate gates. Future gateways must admit AWS health
check source ranges on443; there are no gateways or gateway firewalls in this slice.

Route 53 health checks do not support IAM tag-based authorization. Creation needs
`Resource: "*"`; subsequent operations are scoped to health-check ARNs, and cannot
be isolated by the Project tag. The separate DNS role therefore has account-wide
health-check administration, while record writes remain restricted to the exact
inference A name. Tags express ownership for review, not an IAM enforcement claim.
Before sharing this account with unrelated health checks, narrow existing-resource
permissions to an approved explicit ID allowlist. Never grant zone deletion,
registration changes or arbitrary record writes to this identity.

## Future certificates

The foundation operator will own DNS-01 validation record changes for certificate
issuance and renewal. The current DNS role deliberately permits only the inference
A records; future validation TXT/CNAME names need a separately reviewed narrowly
scoped permission and workflow. Do not give the backend registrar or Route53 write
access. Certificate issuer selection, secure deployment to Lambda gateways,
renewal automation and the compatible gateway/llm-d integration are later work.

Sources: [Route53 permissions](https://docs.aws.amazon.com/service-authorization/latest/reference/list_route53.html),
[weighted health behavior](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/routing-policy-weighted.html),
[HTTPS checks](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/health-checks-creating-values.html).
