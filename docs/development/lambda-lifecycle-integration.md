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

The host-identity component now provides an operation-scoped callback at
`POST /api/v1/bootstrap/{operation_id}`. It uses its own Bearer credential and
cannot be authorized by an administrator session alone. Before launch, `issue`
requires a separately mounted 32-byte bootstrap seed and a committed real pending
intent. It records only the nonce hash, fixed HTTPS origin, seed fingerprint,
20-minute expiry and cloud-init digest. The returned cloud-init is sensitive and
must go directly to the provider; do not retain it in ordinary artifacts or logs.
The deployed configuration has no seed reference yet and cannot issue a launch.

Cloud-init generates a fresh Ed25519 host key, disables password/root SSH, sends
only its public half over verified HTTPS, refuses redirects and bounds retries.
It never transfers the private host key or master seed. The protected nonce file
is removed after acceptance; provider/cloud-init root-only launch records can
retain the short-lived operation nonce until expiry. The receiving API accepts a
callback while dispatch is in flight, before the provider response is committed.
It records the first public key transactionally. Identical retries are idempotent;
a changed key, expired nonce, simulated intent or unattempted/terminated intent
cannot replace it. Callback bodies are bounded to 4 KiB, including chunked input.
The endpoint records host identity only; it never marks a worker ready.

The restricted executor must next combine this retained key with the exact
provider-observed IP, enforce strict SSH verification, and invoke B's typed
prepare/join/remove path. It must not accept arbitrary shell commands, credential
paths or replacement CPU peer definitions from management. Keep its SSH/SSM
privileges outside the management application and artifact mount. Its durable
operation/Job identity must be reconciled after process loss, rather than starting
another host operation blindly. Real UI mode stays disabled until that path and
actual GPU/network readiness are integrated.

The isolated executor now has typed `HostTask`/`HostTaskObservation` contracts and
a tested Kubernetes Job ownership component. Tasks accept GPU/provider identity,
addresses, a trusted public host key, revision/deadline and predecessor identity.
They cannot carry commands, credential paths, manifests or namespace overrides.
The controller fixes the image, non-root UID 20002, private state/credential
mounts, CPU placement and one non-retrying Job. Only an explicit STS-audience
token is projected into the execution Pod; it has no Kubernetes API token.

Inputs and the first observed Job UID are immutable. Repeated/terminal tasks
reuse their original result without API access. An ambiguous submit is observed;
a missing Job fails and cannot be recreated under the same task. Another host
mutation waits for successful completion; failed/uncertain work requires explicit
reconciliation. Changed instance/key/UID, injected initialization containers and
changed Job security/image/command are rejected. This component has cloud-free
ownership tests. Its worker now invokes B's GPU prepare/join/remove operations
under a private state lock, checking the frozen input digest and permanent native
Pod/Job ownership before transport. It verifies the expected Kubernetes namespace
UID and protected CPU peer definitions, pins GPU SSH host access, updates only
the GPU peer, and derives credential references outside management payloads.
Short-lived join material is placed in local scratch and the exact issued token
is revoked afterward. SSH control sockets also use local scratch when configured;
durable receipts and peer state remain on shared storage. Generic host success
cannot mark GPU/connectivity checks ready.

The worker's transport/ownership wrapper has cloud-free tests. Actual SSH/Ansible
execution through this new worker is not yet live-verified. Connectivity/CUDA
inspection, dedicated storage/IAM, deployment, state
handoff and management adapters still require implementation and verification.
The fixed template/core is not an enabled service. Its executor image must include
the pinned operator tools and infrastructure dependencies before deployment.

The internal API and image definition now exist. The only production listener
requires a client certificate signed by the dedicated executor CA; there is no
HTTP fallback. The client fixes the service origin, verifies the server, ignores
proxy environment variables and refuses redirects. Only typed task submission,
observation and public bootstrap metadata are exposed. Request bodies are bounded
to 8 KiB, including chunked input; errors never echo submitted values. Repeated
submissions and an ambiguous native Job response preserve the original task/Job.
This API has offline tests but is not deployed or live-verified yet. The image
uses pinned Python, uv and AWS CLI bases and installs checksum-locked SSM/runtime
artifacts plus the workspace's locked Ansible dependencies. It has a separate
CI image receipt; the existing four-image management release set is unchanged.

Peer-registry ownership must transfer deliberately to the executor. Do not run an
independent operator mesh reconciliation from an older local registry: node-side
revision checks reject stale/conflicting revisions, but they are not a replacement
for one authoritative registry and serialized updates. Fresh-cluster rebuild and
manual reconciliation must first recover that authoritative state. Failed or lost
host work remains cleanup-pending rather than releasing the mutation slot blindly.
