"""A10-only diagnostic view; client delivery/SLO/quality stay in frozen run reports."""

import json
from pathlib import Path

MODEL = '{namespace="llm-d",service="writer-assist-a10"}'
EPP = '{namespace="llm-d",service="writer-assist-a10-epp"}'
GATEWAY = '{namespace="llm-d",pod=~"inference-.*",envoy_http_conn_manager_prefix="http-8080"}'
GPU = '{namespace="gpu-system",UUID="GPU-aa3bb9d3-11f4-f795-1d71-127f61539a1d"}'


def dashboard():
    charts = [
        ("Serving scrape health", 'up{namespace="llm-d"}', "short"),
        ("Ready model endpoints observed by llm-d", "llm_d_epp_ready_endpoints" + EPP, "short"),
        ("Model requests running", "vllm:num_requests_running" + MODEL, "short"),
        ("Model requests waiting", "vllm:num_requests_waiting" + MODEL, "short"),
        (
            "Model completion rate",
            "sum(rate(vllm:request_success_total" + MODEL + "[1m]))",
            "reqps",
        ),
        (
            "Generated tokens per second",
            "sum(rate(vllm:generation_tokens_total" + MODEL + "[1m]))",
            "ops",
        ),
        (
            "Model E2E p95 (server only)",
            "histogram_quantile(0.95,sum by(le)(rate(vllm:e2e_request_latency_seconds_bucket"
            + MODEL
            + "[1m])))",
            "s",
        ),
        (
            "Model queue p95",
            "histogram_quantile(0.95,sum by(le)(rate(vllm:request_queue_time_seconds_bucket"
            + MODEL
            + "[1m])))",
            "s",
        ),
        (
            "Model TTFT p95 (server only)",
            "histogram_quantile(0.95,sum by(le)(rate(vllm:time_to_first_token_seconds_bucket"
            + MODEL
            + "[1m])))",
            "s",
        ),
        ("Model KV cache utilization", "vllm:kv_cache_usage_perc" + MODEL, "percentunit"),
        (
            "llm-d scheduling p95",
            "histogram_quantile(0.95,sum by(le)(rate("
            "llm_d_epp_scheduler_e2e_duration_seconds_bucket" + EPP + "[1m])))",
            "s",
        ),
        ("llm-d errors", "sum(rate(llm_d_epp_request_error_total" + EPP + "[1m]))", "ops"),
        ("Gateway active requests", "envoy_http_downstream_rq_active" + GATEWAY, "short"),
        (
            "Gateway request time p95",
            "histogram_quantile(0.95,sum by(le)(rate(envoy_http_downstream_rq_time_bucket"
            + GATEWAY
            + "[1m])))",
            "ms",
        ),
        (
            "API-key rejections",
            "sum(rate(envoy_http_api_key_auth_unauthorized"
            + GATEWAY
            + "[1m]))+sum(rate(envoy_http_api_key_auth_forbidden"
            + GATEWAY
            + "[1m]))",
            "ops",
        ),
        (
            "EPP poll errors",
            "sum(rate(llm_d_epp_datalayer_poll_errors_total" + EPP + "[1m]))",
            "ops",
        ),
        ("GPU utilization", "DCGM_FI_DEV_GPU_UTIL" + GPU, "percent"),
        ("GPU framebuffer used", "DCGM_FI_DEV_FB_USED" + GPU, "decmbytes"),
        ("GPU temperature", "DCGM_FI_DEV_GPU_TEMP" + GPU, "celsius"),
        ("GPU power", "DCGM_FI_DEV_POWER_USAGE" + GPU, "watt"),
    ]
    panels = [
        {
            "id": 1,
            "type": "text",
            "title": "Experiment boundaries",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 5},
            "options": {
                "mode": "markdown",
                "content": "Retained single A10, Qwen3-4B, private API-key authenticated llm-d. "
                "Baseline and n-gram variants use separate frozen run identities. "
                "Server histograms are diagnostics: authoritative **client completion, "
                "fixed-window SLO, "
                "delivery validity and JFLEG GLEU** are in management run reports. "
                "Non-streaming client requests do not provide client TTFT. No data stays No data; "
                "baseline has no speculation counters. GPU/server availability is not serving HA. "
                "Scraping started after the first baseline began; "
                "retained run records cover its full timeline.",
            },
        }
    ]
    for index, (title, expression, unit) in enumerate(charts):
        panels.append(
            {
                "id": index + 2,
                "type": "timeseries",
                "title": title,
                "datasource": {"type": "prometheus", "uid": "klip-metrics"},
                "gridPos": {"x": 12 * (index % 2), "y": 5 + 8 * (index // 2), "w": 12, "h": 8},
                "targets": [
                    {"refId": "A", "expr": expression, "legendFormat": "{{pod}} {{instance}}"}
                ],
                "fieldConfig": {"defaults": {"unit": unit, "noValue": "No data"}, "overrides": []},
                "options": {
                    "legend": {"displayMode": "table", "placement": "bottom"},
                    "tooltip": {"mode": "multi"},
                },
            }
        )
    return {
        "uid": "klip-a10-speculation",
        "title": "KLIP / A10 WriterAssist speculation",
        "schemaVersion": 41,
        "version": 1,
        "editable": False,
        "timezone": "utc",
        "tags": ["klip", "provisioned", "retained-a10"],
        "refresh": "10s",
        "time": {"from": "now-1h", "to": "now"},
        "panels": panels,
        "links": [
            {
                "title": "Management runs and authoritative reports",
                "url": "https://management.klip-lab.click",
                "targetBlank": True,
            }
        ],
    }


if __name__ == "__main__":
    (Path(__file__).parent / "dashboards/a10.json").write_text(
        json.dumps(dashboard(), indent=2) + "\n"
    )
