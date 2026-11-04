# Real Lambda lifecycle integration

`lambda_mutations.py` implements the bounded transport for Lambda API 1.10.0.
`lambda_ledger.py` coordinates it through C's existing PostgreSQL intent table.
This is an integration component: the deployed real provider/host/cluster/serving
adapters remain unconfigured until the trusted executor and readiness path exist.
The implementation does not rent a GPU merely by importing it or running tests.

The owning management operation first commits a `ProviderIntent` with `mode: real`.
It freezes the typed launch plan, including the exact image, firewall, SSH identity,
region, instance type and cloud-init digest. Bootstrap secret values remain in
separate credential files; they are absent from the plan and provider ledger.
The allocation coordinator validates that digest before reserving capacity.

A globally serialized dispatch transaction admits one Virginia `gpu_1x_a10` and
records the lease deadline, at most 90 minutes, before HTTP. No expensive instance
substitution or region fallback is available. A pending intent has not dispatched;
an unresolved dispatch reserves capacity even when the provider list is empty.
Another project-owned instance with an unknown intent also blocks a new rental.

The account-wide request gate uses a PostgreSQL session advisory lock without an
open transaction during HTTP. It persists minimum 1.05-second request spacing and
12.1-second launch spacing. The capacity refresh adapter must use this same gate
when enabling mutation adapters; its earlier independent polling delay is not an
account-wide limiter. Real transport construction requires a gate.

There is one HTTP attempt. A timeout, 5xx, redirect, malformed response or process
loss leaves dispatch unresolved. Reconciliation adopts only the exact name,
operation/cluster tags, region, instance type and SSH identity; conflicting image
metadata is rejected. A known ID cannot silently change ownership. An empty list
after dispatch never authorizes another POST. Exact capacity, authentication or
rate-limit rejection is terminal for that operation; a new operation can be
submitted after the cause is addressed. Definite rejection releases the reserved
slot. Termination acknowledgment alone does not prove provider disappearance or
release a WireGuard address.

Responses are bounded to 2 MiB and pagination to 32 pages, with duplicate/cycling
page detection. The transport has a fixed HTTPS origin, no HTTP retries, no proxy
environment and no redirect following. It strips Jupyter credentials/URLs and
unknown provider fields before retaining instance observations. Error messages
omit response bodies, request content and credentials.

The remaining integration must connect this component to:

- The UI's explicit real mode and immutable adapter binding.
- Trusted bootstrap host identity and B's restricted prepare/join/remove executor.
- Durable address reservations and serialized complete WireGuard registry updates.
- Short-lived join credentials, node UID checks and actual networking/GPU readiness.
- Lease expiry, ambiguous-allocation reconciliation and confirmed termination.
- Operator-managed GPU software and model releases, with independent readiness.

Keep these gates enforced; the simulated `gpu_ready` state is not acceptable
evidence for a real worker. No schema migration or changes to historical simulated
intents are required by this component.

Verification uses mocked HTTP and an isolated PostgreSQL test database. Tests cover
concurrent dispatch, one-slot admission, ambiguous responses, later reconciliation,
conflicting pins, orphan capacity, credential-field filtering and definite failures.

Official interface checked on 4 November 2026:
[Lambda Cloud API](https://docs.lambda.ai/api/cloud).
