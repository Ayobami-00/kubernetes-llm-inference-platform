# GPU Operator deployment and host admission

The first GPU stage uses plain Ubuntu 24.04 Lambda VMs. Ansible installs host
prerequisites, WireGuard, containerd 2.3.7 and Kubernetes. NVIDIA GPU Operator
26.7.2 owns the NVIDIA driver, Container Toolkit, device plugin, GPU feature
discovery, DCGM exporter and native validators. CPU hosts retain their existing
containerd version.

`deploy/gpu-operator.lock.json` records the chart checksums and Linux AMD64 image
digests. The driver image explicitly selects Ubuntu 24.04. The selected vLLM
image uses CUDA 13.0.2; driver 595.91.07 is within NVIDIA's supported CUDA 13
driver range. This version selection does not establish hardware readiness.

The Operator's bundled NFD is disabled. A separate `gpu-node-features` release
uses the matching upstream NFD 0.20.1 chart/image, including its newer dependency
fixes. Topology updater is disabled. Do not enable a second NFD installation.

## Placement and privilege

Both releases use `gpu-system`. NVIDIA host operands require privileged Pod
admission and host devices/mounts. This exception does not change admission or
credentials in management, runner, data or executor namespaces.

The Operator and NFD master/GC run on platform CPU workers. NFD workers require
the protected `klip.dev.node-restriction.kubernetes.io/gpu-candidate=true` label;
the executor must bind that label to the admitted provider instance and Node UID.
Kubelets cannot assign that protected label themselves. No candidate label is
created by installation. No driver is installed on the six existing CPU nodes.

The bootstrap `NoSchedule` taint remains until actual network/device inspection
passes. NVIDIA operands and NFD workers tolerate it. In the pinned Operator
source, native CUDA/plugin workload validators copy their DaemonSet tolerations,
so they can validate a quarantined host without opening it to serving workloads.

Automatic driver upgrades, MIG, time slicing, sandbox/vGPU, DRA, RDMA, GDS and
GDRCopy are disabled. Changes require a separate reviewed release transition.

Cilium permits private Kubernetes API, DNS and same-namespace component traffic.
Only the NVIDIA driver DaemonSet may download packages over public HTTP/HTTPS.
CPU controllers have no public egress. Only Prometheus in `observability` can
scrape DCGM port 9400 across this boundary. The existing cluster-wide metadata
deny remains in force.

## Install from a saved plan

Run from the repository root after the persistent CPU platform and Prometheus
Operator are ready:

```sh
uv run --locked --group infrastructure python scripts/infrastructure/gpu_operator.py fetch
uv run --locked --group infrastructure python scripts/infrastructure/gpu_operator.py render
uv run --locked --group infrastructure python scripts/infrastructure/gpu_operator_security.py scan \
  --directory artifacts/security/gpu-operator-26.7.2-new
uv run --locked --group infrastructure python scripts/infrastructure/gpu_operator.py prepare \
  --security-directory artifacts/security/gpu-operator-26.7.2
uv run --locked --group infrastructure python scripts/infrastructure/gpu_operator.py apply \
  --approve-plan <approval_sha256>
```

The security directory must contain exact full-image Trivy 0.76.1 reports for
both CPU controllers and `scan-context.json` recording the scanner version,
completion time and vulnerability database metadata. Reports, native scan times
and database freshness are checked against the explicit, expiring review in
`deploy/gpu-controller-risk-review.json`. Critical findings block installation;
remaining High findings must match the exact reviewed tuples. A new scan or
configuration needs an updated review. The review covers CPU controllers only,
not GPU host runtime security or production readiness.

The scan command uses anonymous public registry access and downloads the current
vulnerability database. It refuses to overwrite prior evidence. After a new scan,
review the findings and update the exact report checksums/tuples before preparing
a deployment; do not copy stale reports or advance their timestamps.

Preparation records the source/configuration checksums, six Node UIDs and cluster
UID under the private operator state directory's `cluster/gpu-operator/plans/`.
The initial installer refuses existing GPU/NFD ownership. It installs NFD before
the Operator and retains each Helm log and partial completion state. Changed
inputs, repeated execution, or an existing installation are rejected. A failed
install can leave retained CRDs or a completed NFD release; inspect and reconcile
that exact plan rather than issuing another initial install or deleting history.

Helm/controller readiness proves only that the CPU controllers run. Management
must not mark a worker GPU-ready from this receipt. Operator validation, observed
GPU capacity/memory, a real CUDA computation, DNS/Service/cross-host connectivity
and DCGM telemetry must be verified separately against the exact Node UID.

## References

- [NVIDIA platform support](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/26.7/platform-support.html)
- [CUDA driver compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)
- [Pinned Operator image resolution and validator source](https://github.com/NVIDIA/gpu-operator/tree/5a9c8a8b3f5bac7fb06ad3c2a05771fceb8b4832)
- [NFD 0.20.1 release](https://github.com/kubernetes-sigs/node-feature-discovery/releases/tag/v0.20.1)
