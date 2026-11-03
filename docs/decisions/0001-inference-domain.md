# Inference domain

Date: 3 November 2026

Status: Accepted. Registration succeeded and public delegation matches the existing
hosted zone. Registrar contact verification remains pending.

## Decision

Use `klip-lab.click` as the public domain and
`https://inference.klip-lab.click` as the planned inference endpoint. Keep the
Kubernetes API on the internal NLB's AWS-assigned DNS name, reached through
WireGuard. Choosing this public domain does not change that API decision.

The owner submitted registration through the Route 53 console, supplying their
registrant contact information directly. Route 53 has created the public hosted
zone. Reuse that existing zone; do not create a second zone for the same domain.
The domain and registration-created hosted zone remain account assets, retained
through normal cluster teardown. Terraform will manage the intended inference
records and health checks rather than recreate the zone or registrar registration.

## Registration observations

Read-only checks on 3 November 2026 found:

- Registration operation: `a8afcc34-c06d-4345-94e9-3296da2c2010`, submitted at
  `2026-11-03T03:39:29.951Z`.
- Operation status: `SUCCESSFUL`; registered domain status is `ACTIVE` with
  automatic renewal enabled and expiry on 3 November 2027.
- Public hosted-zone ID: `Z06158312NEW6IHHPH5XV` in account `127811062476`.
- Zone records: the automatically created NS and SOA records only.
- Public delegation matches the four Route53 nameservers below.
- Contact reachability verification is `PENDING`; the owner must complete the
  registrar email verification separately.

The hosted zone's assigned nameservers are:

```text
ns-1595.awsdns-07.co.uk
ns-321.awsdns-40.com
ns-1167.awsdns-17.org
ns-662.awsdns-18.net
```

These are point-in-time observations. Registration and delegation are verified;
contact verification must not be inferred from them. Recheck before publishing
production origins.

## Cost and current observations

The earlier pre-purchase Route 53 API checks on 3 November 2026 reported:

- `klip-lab.click` available, with no existing registration or matching hosted zone
  in the project account.
- Registration: USD 3 for one year; current renewal price: USD 3 per year.
- A Route 53 public hosted zone adds USD 0.50 per month. Base domain and DNS
  hosting therefore total USD 9 per year, excluding tax, queries and health checks.

These are observed prices, not a guarantee of future renewal prices or continuing
availability. The read-only checks did not purchase a domain or create a zone.
See [AWS pricing](https://aws.amazon.com/route53/pricing/).

## Infrastructure interface

The inference DNS Terraform root will accept the existing hosted-zone ID, the
inference hostname and an explicit map of active Lambda gateway origins. Records
will point directly to gateway public IP addresses, with individual health checks.
An empty origin map must not publish fake addresses or create health checks for
nonexistent gateways. Actual serving and failover checks require real gateways;
writing and validating the foundation configuration does not require rented GPUs.

Use separate foundation permissions scoped to this zone and its intended record
names. The management backend does not receive AWS write permissions. Adding and
removing origins remains a foundation workflow.

Public TLS issuance and renewal will be foundation-owned. DNS-based ACME
validation can establish control of the inference hostname before gateway VMs
exist. Certificate delivery, renewal and the eventual gateway integration still
need implementation and live validation. Certificate-automation DNS rights must
not be inherited by the management application.

DNS selects an origin; clients then connect directly to its HTTPS gateway. Retain
the agreed Gateway API and llm-d routing architecture. DNS cache and health-check
delays remain, and existing streams do not migrate when records change.

## Checks after registration

Verify registration status, contact verification, the existing hosted-zone ID and
agreement between registrar and zone nameservers. Confirm public NS delegation.
Only then describe the domain as registered and usable. No inference A records,
gateway certificates or live failover have been configured by this decision.
