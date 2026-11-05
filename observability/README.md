# Observability

The platform uses Prometheus and Thanos for metrics, Grafana for views, Loki for
logs, Tempo for traces, OpenTelemetry for application instrumentation, and DCGM
for GPU measurements. Locked upstream release settings live under `deploy/`;
[`charts/klip-observability`](../charts/klip-observability/README.md) supplies local boundaries.

| Location | Contents |
| --- | --- |
| `grafana/dashboards/` | Provisioned platform, cluster, network, storage, telemetry and A10 dashboards |
| `grafana/build.py` | Platform dashboard generation |
| `grafana/a10.py` | Retained A10 dashboard generation |
| `agents/` | Host WireGuard metric exporter and systemd units |

The A10 view includes serving readiness, running/waiting requests, model/server
latency, gateway/EPP diagnostics, GPU memory/utilization/power and speculative
token acceptance. Baseline decoding has no speculative acceptance denominator;
its counters remain unavailable rather than being displayed as zero.

Client records are authoritative for workload delivery and SLOs. Server histogram
quantiles and scrape-based counter increases are diagnostics. Non-streaming
requests do not yield client TTFT. Prompts, responses and per-request IDs must not
be ordinary metric labels.

See [telemetry deployment and alerting](../docs/runbooks/platform-observability.md),
[private dashboard access](../docs/runbooks/platform-access.md) and
[the measured A10 comparison](../docs/experiments/writer-assist-a10-dashboard-2026-11-04.md).
