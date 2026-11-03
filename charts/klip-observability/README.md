# Telemetry boundaries

Install this chart as `klip-observability` in `kube-system` before the upstream
monitoring releases. It owns the `observability` and `observability-node-agents`
namespaces and their default-deny policies. The deployment operator refuses to
adopt an existing namespace without this exact Helm ownership.

The application namespace enforces the restricted Pod Security Standard. Only the
node-agent namespace allows the read-only host mounts needed by node exporter;
its initial policy admits Prometheus scrapes and grants no egress. Node agents
have no Kubernetes/AWS token, host network, host PID namespace or writable host
mount. Network-namespace collectors are disabled because they would report Pod
counters. Host network and WireGuard instrumentation is a separate prerequisite
for those dashboard signals.

Prometheus, operator and object-metrics API access is explicit. Kube-state-metrics
does not collect or read Secrets/ConfigMaps. The operator watches its release
namespace and the separate node-agent monitor namespace. Additional application
ServiceMonitors should live in `observability` and select the intended target
namespace explicitly.

Only the six exact AWS workload identities get external HTTPS egress for AWS
endpoints. This is a network-layer rule, not a domain filter. Existing global
metadata isolation remains enforced; scoped IAM, projected tokens, TLS and
explicit metadata-fallback disabling provide the other boundaries. DNS uses the
existing CoreDNS path; this chart does not enable Cilium L7/DNS proxying.

All endpoints are private. The chart creates no public Service, ingress, gateway
or application. Later query/log/trace/Grafana releases must add reviewed policies
before relying on connectivity. Provisioned policies require live allowed/denied
connection tests; a successful render is not their acceptance evidence.
