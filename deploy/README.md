# Deployment configuration and pinned releases

This directory contains dependency locks, image/runtime reviews, environment
overlays and release configuration. It is separate from reusable local
[`charts/`](../charts/README.md) and from cloud allocation under `infra/`.

| Location | Responsibility |
| --- | --- |
| `*.lock.json`, `artifacts.lock.json`, `versions.yaml` | Exact upstream charts, images, schemas and artifact digests |
| `environments/lab/` | Reference deployment overlays for management and platform components |
| `environments/lab/experiments/a10/` | Pinned baseline/n-gram profiles, private routing and management experiment settings |
| `*-risk-review.json` | Scoped findings, runtime conditions and expiry checks |
| `helmfile.yaml` | Static CPU cluster add-on ownership |

## Static CPU add-ons

`artifacts.lock.json` records exact release URLs/SHA-256 values and explicit Cilium
CRD sources. `versions.yaml` summarizes the selected versions. Install from the lock
with platform_artifacts.py; mutable commands do not resolve latest versions.

Helmfile owns Cilium/Hubble, metrics-server and the metadata-deny policy. kubeadm owns CoreDNS/kube-proxy,
with explicit bootstrap overrides. CRDs install first with server-side apply;
Cilium operator CRD creation is disabled and Helm receives --skip-crds. Upgrades
must review CRD schema/storage changes explicitly; do not force field conflicts.

Cilium VXLAN uses host WireGuard, Kubernetes PodCIDRs and kube-proxy. No second
WireGuard encryption, ClusterMesh, gateway or inference extension is enabled.
Hubble relay stays private; no public ingress is created. metrics-server verifies
kubelet certificates. CoreDNS and these CPU services have scoped tolerations and
resource requests. Prometheus/Thanos, Grafana, Loki, Tempo, OpenTelemetry and GPU
observability are separate locked releases; see the
[observability runbook](../docs/runbooks/platform-observability.md).

The smoke release remains available for subsequent CPU acceptance. Probe RBAC and
credentials are temporary. Dynamic model releases and management chart templates
have separate release paths and are intentionally absent from this static add-on Helmfile.

## Serving and management

Use [management deployment](../docs/development/management-deployment.md),
[retained A10 serving](../docs/runbooks/retained-a10-speculation.md) and
[dashboard experiments](../docs/runbooks/dashboard-a10-comparison.md) for their
release procedures. The A10 overlay disables provider lifecycle mutations and
uses the host's preinstalled driver/toolkit. Risk reviews are not permanent
approvals; expired or mismatched inputs must block live admission.
