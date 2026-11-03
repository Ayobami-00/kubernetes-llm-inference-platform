"""Provision initial diagnostic views from metrics exposed by the pinned CPU stack."""

from __future__ import annotations

import json
from pathlib import Path

VIEWS = {
    "platform": (
        "Platform overview",
        [
            (
                "Ready Kubernetes nodes",
                'sum(kube_node_status_condition{condition="Ready",status="true"})',
                "short",
            ),
            (
                "Unready application Pods",
                'sum(1-kube_pod_status_ready{condition="true",namespace=~"klip|platform-data|observability"})',
                "short",
            ),
            ("Scrape targets down", "sum(1-up)", "short"),
            ("Firing alerts", 'sum(ALERTS{alertstate="firing",alertname!="Watchdog"})', "short"),
            (
                "Platform CPU requests",
                'sum(kube_pod_container_resource_requests{resource="cpu",node=~"platform-.*"})',
                "cores",
            ),
            (
                "Platform memory requests",
                'sum(kube_pod_container_resource_requests{resource="memory",node=~"platform-.*"})',
                "bytes",
            ),
        ],
        (
            "This view covers the CPU foundation. GPU/model/gateway/customer "
            "delivery sources are added only after their pinned exporters and "
            "authoritative records are integrated. Pod Ready is not GPU or "
            "serving readiness."
        ),
    ),
    "cluster": (
        "Cluster and host diagnostics",
        [
            (
                "Host CPU utilization",
                '1-avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m]))',
                "percentunit",
            ),
            (
                "Host memory utilization",
                "1-node_memory_MemAvailable_bytes/node_memory_MemTotal_bytes",
                "percentunit",
            ),
            (
                "Host filesystem available",
                'node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"}',
                "bytes",
            ),
            (
                "Container CPU throttling",
                (
                    "sum by(namespace,pod)(rate(container_cpu_cfs_throttled_perio"
                    'ds_total{namespace!=""}[5m]))/sum by(namespace,pod)(rate(con'
                    'tainer_cpu_cfs_periods_total{namespace!=""}[5m]))'
                ),
                "percentunit",
            ),
            (
                "Container restarts",
                "sum by(namespace,pod)(increase(kube_pod_container_status_restarts_total[15m]))",
                "short",
            ),
            (
                "Kubernetes API request rate",
                "sum by(verb,code)(rate(apiserver_request_total[5m]))",
                "reqps",
            ),
        ],
        (
            "Node exporter reads host CPU/memory/filesystems. Its Pod-network "
            "counters are disabled. WireGuard, Cilium/Hubble, authenticated etcd "
            "and private scheduler/controller metrics are pending; no network or "
            "etcd health is inferred from absent data."
        ),
    ),
    "storage": (
        "PostgreSQL and persistent storage",
        [
            (
                "Database Pod readiness",
                'kube_pod_status_ready{namespace="platform-data",pod=~"klip-db-[0-9]+",condition="true"}',
                "short",
            ),
            (
                "PVC available capacity",
                'kubelet_volume_stats_available_bytes{namespace=~"platform-data|observability"}',
                "bytes",
            ),
            (
                "PVC utilization",
                '1-kubelet_volume_stats_available_bytes{namespace=~"platform-data|observability"}/kubelet_volume_stats_capacity_bytes{namespace=~"platform-data|observability"}',
                "percentunit",
            ),
            (
                "Host disk read latency",
                "rate(node_disk_read_time_seconds_total[5m])/rate(node_disk_reads_completed_total[5m])",
                "s",
            ),
            (
                "Host disk write latency",
                "rate(node_disk_write_time_seconds_total[5m])/rate(node_disk_writes_completed_total[5m])",
                "s",
            ),
            (
                "Database container restarts",
                (
                    "sum by(pod)(increase(kube_pod_container_status_restarts_tota"
                    'l{namespace="platform-data"}[15m]))'
                ),
                "short",
            ),
        ],
        (
            "Storage metrics are host/device or kubelet observations. PostgreSQL "
            "replication, SQL latency and backup/archive freshness require the "
            "CNPG metrics integration; Pod readiness does not establish those "
            "properties. Independent backup/restore evidence remains in the "
            "operator ledger."
        ),
    ),
    "telemetry": (
        "Telemetry delivery and storage health",
        [
            ("Targets down by job", "sum by(job)(1-up)", "short"),
            ("Samples scraped per target", "scrape_samples_scraped", "short"),
            ("Prometheus head series", "prometheus_tsdb_head_series", "short"),
            ("Collector queue size", "otelcol_exporter_queue_size", "short"),
            (
                "Collector exported spans",
                "sum by(exporter)(rate(otelcol_exporter_sent_spans[5m]))",
                "ops",
            ),
            (
                "Collector exported log records",
                "sum by(exporter)(rate(otelcol_exporter_sent_log_records[5m]))",
                "ops",
            ),
            (
                "Alert notification failures",
                "sum by(integration)(rate(alertmanager_notifications_failed_total[5m]))",
                "ops",
            ),
        ],
        (
            "Thanos deduplicates Prometheus replicas; partial query responses are"
            " disabled. Missing series remain No data. Queue units and actual "
            "Collector metric names must be checked against the installed binary "
            "before telemetry delivery acceptance. This view is not proof of "
            "object retention or lossless delivery."
        ),
    ),
}


def build():
    root = Path(__file__).resolve().parent / "dashboards"
    root.mkdir(parents=True, exist_ok=True)
    for name, (title, charts, note) in VIEWS.items():
        panels = [
            {
                "id": 1,
                "type": "text",
                "title": "Scope and interpretation",
                "gridPos": {"x": 0, "y": 0, "w": 24, "h": 4},
                "options": {"mode": "markdown", "content": note},
            }
        ]
        for index, (label, query, unit) in enumerate(charts):
            panels.append(
                {
                    "id": index + 2,
                    "type": "timeseries",
                    "title": label,
                    "datasource": {"type": "prometheus", "uid": "klip-metrics"},
                    "gridPos": {"x": (index % 2) * 12, "y": 4 + (index // 2) * 8, "w": 12, "h": 8},
                    "targets": [
                        {
                            "refId": "A",
                            "expr": query,
                            "legendFormat": (
                                "{{instance}} {{namespace}} {{pod}} {{job}} {{exporter}}"
                            ),
                        }
                    ],
                    "fieldConfig": {
                        "defaults": {"unit": unit, "noValue": "No data"},
                        "overrides": [],
                    },
                    "options": {
                        "legend": {"displayMode": "table", "placement": "bottom"},
                        "tooltip": {"mode": "multi"},
                    },
                }
            )
        dashboard = {
            "uid": "klip-" + name,
            "title": "KLIP / " + title,
            "schemaVersion": 41,
            "version": 1,
            "editable": False,
            "timezone": "utc",
            "tags": ["klip", "provisioned", "cpu-foundation"],
            "refresh": "30s",
            "time": {"from": "now-1h", "to": "now"},
            "panels": panels,
            "links": [],
        }
        (root / (name + ".json")).write_text(json.dumps(dashboard, indent=2) + "\n")


if __name__ == "__main__":
    build()
