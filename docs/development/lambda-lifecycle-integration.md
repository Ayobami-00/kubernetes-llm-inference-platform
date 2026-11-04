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

The infrastructure plan was applied on 4 November 2026: a separately encrypted,
retained EFS filesystem/access point (UID/GID 20002, path mode 0700), three mount
targets restricted to the existing platform CPU security group, and a workload
role bound only to `platform-ops:klip-host-job`. Its only permissions start SSH
sessions on the six allocated CPU hosts and end sessions with the same federated
owner tag. This follows AWS's [federated session ownership policy](https://docs.aws.amazon.com/systems-manager/latest/userguide/getting-started-restrict-access-examples.html).
The management namespace has no binding for this role or claim. Image-pull trust
adds exactly the two executor service accounts; publisher permissions are unchanged.
Saved plan digests: storage `a1accdbd77ecd68a719137915f06b286c87e25ee4351bd97e1afddb8acbb62fa`,
access `f55e4b45e3da72c9fe28366e0b80ae03fc9c5bcabdfdb198199b812f9188f8b2`,
registry `68c570f229a3802e7801f5ba62a620c47b801a7fba4ee1fd18d59673b9867b12`.
Allocation does not establish successful mounts or executor deployment.

The first executor image (`0bd844d`) passed ECR's scan with eight High findings
and no Critical findings. A separate Trivy 0.76.1 full-image scan also inspected
Python, Go and Rust packages and found four Critical findings. That image is
rejected for deployment. The replacement removes uv from runtime, moves to a
verified Python Trixie base and builds only the checksum-verified OpenSSH 10.6p1
client binaries. Debian stable's client remains vulnerable to the host-key
re-exchange issue according to its [security tracker](https://security-tracker.debian.org/tracker/CVE-2026-60002);
the upstream [OpenSSH release](https://www.openssh.org/releasenotes.html) and checksum
are recorded in the build helper. A new full scan and an independent exact-image
runtime review must pass before deployment. No finding is silently suppressed.

Replacement source `6cfd742` built successfully. Its ECR scan has three High,
nine Medium and three Low findings. The complete Trivy scan has no Critical
findings and 55 High package/version tuples (including repeated source-package
attribution). `deploy/host-executor-risk-review.json` retains each tuple and its
runtime rationale, expires on 11 November, and applies only to the isolated CPU
executor. This does not approve the retained runc artifact for live GPU use or
declare these dependencies fixed. The first rejected image and scan remain
retained. The SSH session policy's follow-up saved plan
`2df3b6b2262f673904f859aadb4ee020752d640760b1304f5431557e976ae872`
adds only the required data channel on the fixed executor session-name prefix.

`host_executor_release.py handoff` freezes the six-host public inventory, host-key
pins, cluster namespace UID, peer registry and public bootstrap key under private
operator state. SSH private keys are excluded from that file bundle. The executor
CA key also stays on the operator machine; separate server/client leaf identities
are staged into different namespace Secrets. `prepare` checks the exact shipped
source against the image commit, the complete scan, current review and separate
storage identity. It freezes credential public fingerprints and the rendered chart.

The following initial-install workflow requires the exact reviewed receipt and
scan directory; `stage` and `deploy` require its printed approval digest:

```sh
.venv/bin/python scripts/infrastructure/host_executor_release.py handoff
.venv/bin/python scripts/infrastructure/host_executor_release.py prepare \
  --receipt artifacts/platform/host-executor-6cfd742/host-executor.json \
  --security-directory artifacts/security/host-executor-6cfd742
.venv/bin/python scripts/infrastructure/host_executor_release.py stage --approve-plan <digest>
.venv/bin/python scripts/infrastructure/host_executor_release.py deploy --approve-plan <digest>
```

Staging uses an independently identified, non-retrying CPU Job; it never submits
a fake GPU task. It verifies the mounted private filesystem, STS workload identity,
cluster UID and strict SSH/sudo transport to all six existing CPUs. It then fences
the old operator registry. A lost staging Job is not recreated, and different
retained credentials/files cannot be overwritten on a retry. Namespace/Secret/Job
UIDs and the preflight result remain in the private release receipt. Only then may
the two-replica API be deployed. These scripts currently implement initial custody
installation; upgrades and fresh-cluster recovery require a separate explicit,
history-preserving procedure. Running `prepare` cannot erase a staged/live release.
Management adapters, GPU inspection and failure cleanup remain separate gates.
