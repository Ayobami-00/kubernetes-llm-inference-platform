# Real Lambda lifecycle integration

The current single-A10 workflow now includes a separate
[GPU/network inspection implementation](../runbooks/gpu-worker-inspection.md),
measured device observations and an inspection-configuration gate before a new
paid dispatch. These paths are covered by synthetic external-I/O tests. The live
executor has its reviewed inspection-capable image upgrade; the separate GPU
runtime-approval receipt remains required. Free provider prerequisites were
registered and verified on 4 November. Real UI provisioning remains disabled
until runtime approval and the lifecycle configuration are deployed. The earlier
sections below record the component's implementation
and live integration history, rather than claiming a real GPU has been verified.

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

New real launches freeze a `klip-host-attestation.v2` bootstrap policy containing
the three executor CPU public `/32` addresses. Before reporting its host key,
cloud-init verifies the effective OpenSSH configuration: only `ubuntu` from those
addresses is allowed, root/password/interactive authentication is disabled, and
the checked configuration has been activated. Policy failure prevents the
attestation callback and leaves the worker quarantined. Historical v1 documents
remain byte-compatible; existing intents cannot change their frozen SSH sources.
Lambda's account-wide firewall rules still allow TCP 22 from the Internet and
are deliberately unchanged. This safeguard restricts SSH authentication; the
subsequent Ansible host firewall and actual negative-connectivity checks remain
required. The v2 program has local execution tests, not live GPU-host evidence.

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

The worker's transport/ownership wrapper has cloud-free tests. The isolated API,
storage/IAM, image and CPU transport preflight are now deployed and live-verified
as described below. Actual GPU-host Ansible execution, connectivity/CUDA inspection
and management lifecycle adapters still require implementation and verification.

The internal API and image definition now exist. The only production listener
requires a client certificate signed by the dedicated executor CA; there is no
HTTP fallback. The client fixes the service origin, verifies the server, ignores
proxy environment variables and refuses redirects. Only typed task submission,
observation and public bootstrap metadata are exposed. Request bodies are bounded
to 8 KiB, including chunked input; errors never echo submitted values. Repeated
submissions and an ambiguous native Job response preserve the original task/Job.
This API passes offline and live authentication/RBAC checks. The image
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
  --receipt artifacts/platform/host-executor-4ed4402/host-executor.json \
  --security-directory artifacts/security/host-executor-4ed4402
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

The first live preflight mounted the dedicated EFS volume on `platform-03` as UID
20002 and retained the public handoff files, but failed before SSH because Debian's
`/var/run` symlink caused its projected token mount to shadow the credential mount.
Job UID `cb124d6c-8b38-4aa3-b108-b4df2dedc19e` was retired with a UID-checked deadline
update and reached native failure. Its original plan, Job and failure receipt are
preserved. No GPU allocation or host configuration occurred, and the operator's
registry was not transferred. The correction uses distinct credential/identity
directories and an image-bound AWS profile. Regression tests normalize `/var/run`
to `/run` before checking mount uniqueness. The existing immutable credential
Secret keeps its original unused profile field; execution uses the new profile
in the reviewed image, so retries do not rotate or overwrite credentials.

The following attempt inherited the API's 512 MiB container limit and was
OOM-killed. Native Job `c474707c-7f3f-4b0f-882e-f6298ebb3a53` failed without retry.
The preflight now uses the real host-Job envelope (500m CPU/512 MiB requests,
2 CPU/2 GiB limits). Attempt `0554d01912f6dcc73a4740ebb6e961476012d061f67290c22b02b9829d1ef95a`
passed at 03:59:38 UTC on 4 November: EFS, scoped STS identity, expected Kubernetes
namespace UID and strict SSH/sudo on all six CPUs were verified. Its native Job UID
is `978ded93-6130-43df-ab73-84ff6a359d41`. The old operator's peer writer is now
fenced; the authoritative registry is the executor filesystem, revision 12.

The two API replicas deployed at 04:00:50 UTC on `platform-02` and `platform-03`.
Live boundary verification first found CIDR-only API egress blocked by Cilium's
default exclusion of node identities. The chart now supplements it with the
`kube-apiserver` entity on TCP 443/6443, following the
[Cilium policy model](https://docs.cilium.io/en/stable/security/policy/layer3/).
The policy-only saved update `00634089010e13baa2d75fc8908332dbcfba9666359503a02147403ed9f427b2`
left every existing resource unchanged, added that single rule, and deployed at
04:09:01 UTC. The original release, update and failed verification remain retained.

Verification `e4fa9b7102cc` passed at 04:09:53 UTC. A credential-minimal Job in `klip`
accepted the valid client certificate, rejected absent client identity and an
untrusted server CA, rejected malformed tasks without echoing input, and had no
SSH credentials mounted. The actual API workload token read the expected native
Job; authorization denied Secret reads, Job creation in `klip` and Node patches.
Both unchanged API Pods remained ready on distinct CPU hosts. The private receipt
is `cluster/host-executor/acceptance/e4fa9b7102cc/result.json` under operator state.

Management's real lifecycle bindings remain disabled. No GPU has been rented and
this checkpoint does not establish Ansible/GPU readiness or complete platform
production readiness. Next work is durable management integration, actual GPU
inspection, failure cleanup and the UI-driven single-A10 lifecycle.

Capacity refresh now shares the PostgreSQL account-wide request gate with
mutation calls. The existing observation-refresh lock still prevents duplicate
refresh jobs; it no longer serves as independent provider pacing. Production
reads refuse redirects and environment proxies, bound each response to 2 MiB and
pagination to 32 pages, and reject repeated cursors or duplicate/missing instance
identities. Only allowlisted instance fields are retained. Cloud-free tests cover
gate ownership, secret-field filtering, redirects, oversized/malformed responses
and pagination failures. Real allocation remains disabled until its full durable
lifecycle and failure cleanup are integrated.

A read-only catalog observation on 4 November confirmed Virginia A10 capacity at
129 cents/hour and plain Ubuntu 24.04 image
`6c389693-2b2d-47a3-93da-052c2331bd98` (`ubuntu-24-04`, `24.4.2-20250626`, x86_64).
The private metadata receipt is `cluster/host-executor/lambda-catalog.json` under
operator state. These are candidates for the next integration step; availability,
image compatibility and price must be rechecked before launch. No provider resource
was created by the catalog query.

Failed bootstrap cleanup now has separate typed safety and revocation actions.
`cleanup-check` requires the exact Lambda node identity, its bootstrap taint and
no active non-DaemonSet workloads. It cordons with UID/resource-version tests.
A ready/unquarantined worker must use normal drain/removal instead. Completed
safety checks retain the executor's mutation slot for their cleanup chain.

Management persists trusted safety/removal evidence before termination intent.
`terminate_once` checks a complete provider list and the exact-instance read;
only mutually consistent terminal states or documented exact-object absence
produce a `ProviderFence`. A successful termination POST alone cannot do so.
An absent instance and an active instance in conflicting reads remain unresolved.
The documented API semantics are recorded in the
[Lambda Cloud API](https://docs.lambda.com/api/cloud).

After provider fencing, `revoke-bootstrap` never SSHes to the terminated host.
It checks the quarantine/workload state again, removes only the bound peer and
inventory entry, reconciles remaining direct peers, and deletes the node with its
UID precondition. Failure keeps cleanup pending. A fresh recovery task may retry
that bounded revocation with the same fence; lost original Jobs are not restarted.
The provider ledger retains the allocation slot while `fenced` and releases it
only on matching completed executor evidence. Normal drained removal uses its
original receipt after provider fencing. Historical launch/termination calls reuse
their committed identities/receipts without new bootstrap or provider I/O.

These changes retain schema version 0.1 and old task input digests. The cleanup
actions and provider ledger have offline ownership/transport tests and dedicated
PostgreSQL tests. They have not yet been deployed into the executor or exercised
against a real GPU allocation; real management lifecycle remains disabled.

Management now has a PostgreSQL host-task journal in the existing provider intent.
`DurableHostTasks` commits inputs and the active chain before calling the mTLS
executor. It records the native Job identity and observations; another management
process resumes that same task. Terminal observations are retained history and do
not construct a new client or reload TLS credentials. Conflicting inputs, changed
UIDs, unowned instances and invalid lifecycle transitions are rejected. The
executor and management use the same shared follow-up validator.

WireGuard reservations are serialized in PostgreSQL and bound to the executor's
cluster UID and peer revision. They skip both current executor peers and unresolved
database reservations. Failed/fenced allocations retain their addresses until
cleanup completes. A task cannot use a different or older reservation. This
reserves addresses; only the isolated executor writes the authoritative peer mesh.

If host attestation fails before any host task was recorded, a separate
`unbootstrapped` authorization permits provider termination without fabricating
an SSH identity or node observation. That proof requires an explicitly empty task
journal; missing historical fields do not count. Authorization and first dispatch
lock the same intent, so they cannot both succeed. Provider fencing remains
mandatory before releasing that reservation. Once a task is frozen, the regular
safety-check/revocation path is required even if its HTTP response was lost.

An input whose absolute deadline expired before native Job submission returns a
stable failed observation (`host_task_expired_unsubmitted`). Repeating that input
cannot create a Job, including after a lost initial connection. A fresh cleanup
check can follow it. If a native submission receipt already exists, the original
Job/receipt remains authoritative; this expiration path cannot replace it.

Verification checkpoint: the final offline suite passed 853 tests (17 optional
skips), including deadline recovery. Management's complete backend/PostgreSQL,
web and Kubernetes integration CI passed for both `f89c817` and `4a96232`,
including the concurrent ownership, address, fencing and journal tests. Ruff,
formatting, JSON Schemas and management OpenAPI checks also passed. Local Docker
did not answer a bounded daemon query; only that query was stopped, and the
PostgreSQL tests ran in CI's isolated service instead.

The management queue now has a separate real-worker workflow in
`platform_management.jobs.real_worker`. It uses the durable provider/host journals
for allocation, attestation, preparation, join, GPU inspection, renewal and cleanup.
No simulator adapter or hardcoded GPU-ready transition is used on that branch.
Unknown launch responses reconcile the original intent. Normal pending host
observations remain pending without exhausting the generic exception retry count;
the persisted workflow deadline still bounds unavailable dependencies.

The explicit `MANAGEMENT_REAL_LIFECYCLE_ENABLED` switch defaults to false. Enabling
it requires a validated policy file, separate 32-byte bootstrap seed reference,
and dedicated executor client TLS mount. Immutable execution bindings retain the
public policy, cluster/public-key identity, TLS fingerprints and credential paths;
secret values are excluded. Existing resource bindings remain the authority for
cleanup even if the current switch is subsequently disabled.

The policy permits one Virginia A10, a pinned plain Ubuntu image/version and
explicit provider SSH/firewall identities. Offering, price, architecture, GPU
count, regional capacity and image metadata are rechecked before the first POST.
Initial leases are at most 90 minutes. Explicit renewal operations are idempotent,
at most 90 additional minutes each and at most 180 total minutes by default; no
automatic renewal or replacement rental is introduced. Host setup deadlines leave
a cleanup margin before expiry. Price remains bounded to the reviewed 129 cents
per instance-hour for this gate.

After a lost management checkpoint, the error/expiry paths recover committed
instance IDs and lease timestamps from the provider ledger. Termination and
cleanup remain observable obligations rather than disappearing with a failed
operation. A failed GPU inspection with an already bound node UID can use normal
drain/removal; it cannot force provider termination past a drain failure.

The real creation API now accepts explicit `mode: real`, rejects simulation
faults, and requires a compatible pinned group specification and configured real
binding. OpenAPI and the generated TypeScript types follow that actual route.
The provisioning controls and mounts described below are encoded in the repository;
actual GPU inspection and runtime enablement remain integration work. The default
deployed policy remains disabled and no rental is established by these tests or
code changes.

The capacity page now offers an explicit live-provisioning form. Its options come
from `GET /api/v1/worker-lifecycle-options`; only published specifications matching
the single-A10 policy are selectable. The lease is the selected specification's
recorded value. Editing/publishing another profile supports another approved lease
without silently rewriting a versioned configuration. Fresh provider capacity,
regional availability, a known price within the ceiling and no unresolved live
reservation are required in the UI; server preflight remains authoritative.

`charts/klip` has optional lifecycle configuration and credential mounts. They are
disabled by default and restricted to the `klip` namespace. Only API/worker Pods
receive the executor client identity and bootstrap seed. Web, migration and runner
Pods receive neither; privileged host/server credentials remain in `platform-ops`.
The chart hashes the public policy into the Pod template to roll changed settings.

Bindings also retain the provider credential fingerprint. A changed credential
cannot be used to interpret another account's empty inventory as successful
termination. Keep the original credential available until its allocations are
cleaned up; account/credential rebinding requires an explicit audited procedure.
No raw credential values enter the binding or retained task inputs. Simulated
serving deployments are rejected against live worker groups.

Workflow commit `5b24308` passed management backend/PostgreSQL, web and Kubernetes
integration CI (`41776947204`). That proves the mocked external-I/O state-machine
integration, including confirmed cleanup and lost-checkpoint recovery; it does not
establish a real GPU-host run. Actual GPU inspection, deployment enablement and the
live single-A10 acceptance gate remain outstanding.
