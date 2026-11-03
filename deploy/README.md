# Static CPU platform releases

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
resource requests. Prometheus/Grafana/Loki/Tempo/OTel and GPU observability follow.

The smoke release remains available for subsequent CPU acceptance. Probe RBAC and
credentials are temporary. Dynamic model releases and management chart templates
belong to C and are intentionally absent from this Helmfile.
