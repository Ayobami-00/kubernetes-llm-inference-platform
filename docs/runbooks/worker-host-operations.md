# Worker host operations for Stream C

The implemented base host profile is Ubuntu24.04 AMD64. Unsupported images fail
before host changes; validate Lambda image availability and GPU software in the
later GPU milestone. This slice tests the generic path on the separate AWS VM.

B exposes `cluster.py worker-prepare|worker-join|worker-remove --request FILE`.
Requests use klip-worker-operation.v1 and results use klip-worker-result.v1. See
WorkerRequest in scripts/infrastructure/cluster_models.py for the exact fields.
C owns allocation/termination, operation persistence, distributed serialization,
address allocation and job retries. B's initial operator executor uses a local
lock; it is not a replacement for C's durable/distributed coordination.

The explicit GPU preparation variant uses `klip-worker-operation.v2`, parsed as
`GPUWorkerRequest`. It requires `runtime_profile: nvidia-operator`, Lambda and
`quarantine: true`. V1 remains the generic/CPU profile with its existing serialized
form and idempotency hashes. The v2 path stages the checksum-locked containerd
2.3.7 archive before host changes, rejects a preinstalled NVIDIA stack and prepares
kernel/runtime prerequisites. NVIDIA software is installed later by GPU Operator;
generic join still reports `gpu_runtime: false`. See `deploy/gpu-host.lock.json`
and `docs/development/a10-compatibility-review.md`. No operator installation or
live GPU verification is implied by successful host preparation.

Requests carry cluster/provider identity, node name/expected UID, addresses,
credential references, current peer-registry revision, operation ID and deadline.
Lambda SSH access additionally requires explicit executor public /32 sources;
there is no broad public SSH rule. AWS CPU/probe administration stays SSM-only.
The Lambda provider must permit the initial SSH connection from those sources.
B installs a matching host restriction so later reconciliation remains reachable.
AWS requests may include a verified `availability_zone`, which is preserved in the
kubelet provider ID. `quarantine: true` registers new nodes with the bootstrap
`NoSchedule` taint. Only the trusted caller removes that taint after its checks.

Resolve access aliases from the executor's private cluster/credential-references.json.
Each entry contains ansible_host, ansible_user, ansible_ssh_private_key_file and
ansible_ssh_common_args with strict known_hosts pinning. Resolve join_ref from
cluster/join-references.json to a private file containing expires_at and discovery
(apiServerEndpoint, token, caCertHashes). Issue short-lived credentials outside job
payloads; do not give C B's admin.conf. Automatic join-token issuance/authentication
and deployment of C's durable executor are the next integration work, owned jointly
at the interface; this slice does not implement C's application or job service.

Prepare returns the host's public WireGuard key. C allocates a reserved .64-.254
address, publishes a complete klip-peers.v1 registry at the next revision, and invokes
mesh reconciliation before join. Peers use direct /32 paths. Registry updates reject
stale revisions, duplicate identities/keys/addresses and changes to fixed CPU slots.
Persistent platform workers reserve `.64` through `.66`; dynamic GPU allocation
starts at `.67` and must also consult the current registry and durable reservations.
The API prefixes move independently under the host route-helper lock.
Address reconciliation removes an obsolete IPv4 assignment before re-establishing
the registry's sole address. This also handles Linux removing secondary addresses
when an old primary is removed. Acceptance checks direct gateway probes again after
the temporary peer changes from `.2` to the reserved worker address `.64`.

The initial B executor can reconcile CPU hosts through its own MFA/SSM identity.
A C-facing executor must retain that foundation credential boundary: invoke B's
privileged host-operation interface, rather than injecting AWS write credentials
into the management backend. The request/result format supports that separation;
a separately deployed/authenticated executor endpoint is not included here.

Join is generic Kubernetes preparation. It returns actual Kubernetes name/UID and
observed host/WireGuard/Kubernetes readiness; GPU runtime remains false. Driver,
Toolkit, device plugin, DCGM and model readiness belong to the later GPU milestone.
The temporary AWS acceptance worker is never emitted as a Lambda GPU inventory node.
Before reporting Cilium readiness, join validates and approves only known kubelet
serving CSRs, then verifies the installed serving certificate against the cluster
CA. This uses B's privileged executor; C does not receive blanket CSR approval rights.

Removal requires matching provider ID and node UID, respects drain/PDB/deadline
failures, stops kubelet, disables API routing, revokes direct peers, then deletes
with an API UID precondition. Repeated operation IDs with different inputs are
rejected. Failure records keep cleanup_pending true. A completed host removal still
returns address_reusable false: C must confirm provider termination before releasing
that address. No forced drain, provider termination or silent identity replacement
is performed by B.
