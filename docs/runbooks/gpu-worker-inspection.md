# Real GPU worker inspection

The isolated executor's `inspect` action is separate from generic Kubernetes join.
It is currently restricted to the single-A10 profile. A successful join, a Ready
Node, or a Ready ClusterPolicy does not establish GPU readiness.

## Admission sequence

The executor holds its existing private operator lock and validates the frozen
task, trusted provider/SSH identity, authoritative peer revision, Kubernetes
namespace UID and node UID. Before labelling a candidate it requires an
operator-staged `cluster/gpu-runtime-approval.json` on its private retained volume.
That receipt binds the cluster, current runtime review, complete GPU/base artifact
locks, image digests, operator identity/configuration and review expiry. Missing
or expired approval leaves inspection unconfigured. Management checks that state
before a new paid dispatch; cleanup never depends on an active runtime review.

Inspection then checks:

1. Host runtime versions, synchronized time and available storage through B's
   existing host preflight.
2. Direct WireGuard connectivity in both directions to every permanent CPU host,
   fresh expected handshakes and a non-fragmenting WG1420 packet.
3. The exact pinned Operator/component configuration. Candidate labels bind the
   provider instance and Node UID under the protected NodeRestriction prefix.
4. Ready native driver, toolkit, device-plugin, feature-discovery, exporter and
   validator DaemonSet Pods, including actual runtime image IDs.
5. Exactly one advertised and allocatable GPU.
6. A real CUDA vector-add Pod, preceded by `nvidia-smi` in an init container.
   Init/main reserve one GPU sequentially. The device must be an A10 with the
   pinned driver and sufficient actual usable memory.
7. Seven short-lived network Pods: one on each CPU host and one on the admitted
   GPU. They verify 42 directed cross-Pod connections and seven DNS/Service paths.
   The GPU Pod's exact CRI sandbox UID establishes interface/route MTUs of
   1420/1370; a stale sandbox with a matching name cannot substitute for it.
8. Fresh UUID/exporter-bound DCGM samples scraped by both Prometheus replicas.
   Administrative loopback forwarding queries Prometheus; it does not replace
   exporter ingestion. Missing samples remain missing, never zero.
9. Unchanged node identities, Operator configuration and runtime approval.

The full proof is persisted before promotion. A UID/resource-version-guarded
patch then removes only the bootstrap taint and adds protected readiness/latency
placement labels. Other taints remain. No earlier failure path promotes the node.

## Results and partial cleanup

The native task has one permanent Job/Pod owner and an absolute deadline. It
never restarts automatically. Probe Pods use `restartPolicy: Never`, bounded
active deadlines, non-root security and no API token or host mounts. Their
manifests are frozen before creation. Resource names, input hashes and actual
UIDs guard cleanup. An explicit later removal walks the original task chain to
clean probes retained after executor loss; it cannot adopt another task's files.

Private task artifacts include:

- `probe-manifests.json`, `probe-evidence.json`, and `cuda-output.txt`.
- `inspection-proof.json`, written before removing quarantine.
- `inspection-evidence.json`, including the final node observation and telemetry.
- The existing atomic task observation and private diagnostics on failure.

The shared observation and worker member carry optional measured device facts:
UUID, actual name, usable MiB, driver version, allocatable count and timestamp.
The UI presents these as observations, separately from the provider's advertised
VRAM. Missing fields remain explicit. Null additions are omitted from historical
receipt/member serialization so existing cleanup evidence hashes remain stable.

## Deployment status and remaining gates

The inspection implementation and tests use synthetic external observations;
they do not establish successful execution on a real GPU. Operator/NFD CPU
controllers and GPU scrape-policy wiring are live. On 4 November at 09:27 UTC,
the reviewed inspection image was deployed while preserving executor custody.
At 09:30 its cluster-bound runtime assessment was staged and verified.
Management's bounded lifecycle configuration deployed at 09:32 and passed
HA/database/artifact verification at 09:38. The public UI then published and
verified `latency-workers@2`. Actual allocation, host execution and GPU inspection
still require their own retained evidence; these setup steps do not imply
that a GPU is ready.

The opt-in `GPU runtime image security` workflow scans exact public AMD64 images
on isolated CPU runners, without cloud/registry credentials. Raw reports remain
unchanged even when findings require review. Its first run, `41786403761` at
source `75d0ec7`, scanned all eight images. Seven completed without Critical
findings. The driver scan reported 22 Critical kernel findings on
`linux-libc-dev@6.8.0-138.138`; its recorded installed-file list contains userspace
development headers and documentation. Image finding applicability and the
actual host kernel require separate explicit review. No GPU runtime approval is
implied by header-only package evidence or this documentation.

Fresh-host Ansible preparation pins Ubuntu GA `6.8.0-147-generic` and matching
`6.8.0-147.147` image, headers and modules. It refuses joined hosts, unexpected
kernel families and downgrades, disables Nouveau in the initramfs, and performs
a bounded reboot when needed. This runs before the general host role. Inspection
reads the actual running kernel, dpkg package states, disabled GCOV configuration
and boot ID before admission and again before promotion. A reboot or changed
kernel/package evidence keeps the bootstrap quarantined. The kernel pin is not a
claim that all published kernel vulnerabilities are fixed. Remaining host-kernel
applicability and exposure must be recorded in the runtime review.

The executor image verifies its installed, checksummed archives and its actual
inspection imports/configuration during the build without cloud or host access.
This includes the public workload-identity configuration. GPU containerd is
staged at build time, so a read-only runtime filesystem needs no artifact download.

The host's kernel prerequisites and NVIDIA operands must be validated on the
actual fresh VM. CUDA 12.5 vector addition establishes device execution; the
pinned vLLM CUDA 13.0.2 deployment has its own subsequent serving acceptance.
