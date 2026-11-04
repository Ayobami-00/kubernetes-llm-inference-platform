# Retained A10 WriterAssist experiment

This workflow is limited to the user's manually retained `gpu_1x_a10` instance
`8f2e29b7279b42148ffaaed4a4dcdd8e`, Kubernetes node `writer-a10-retained`.
**Never terminate, reboot, replace, renew or attach an automatic expiry to this VM.**
A failed software attempt retains its evidence; it does not authorize an automatic
restart. No other model profiles or provider capacity are part of this experiment.

The host uses its existing Lambda Stack driver/toolkit and original Docker service.
An isolated `klip-containerd` service provides Kubernetes CRI without replacing that
runtime. The GPU Operator manages validation, discovery, the device plugin and DCGM;
its driver and toolkit installation operands remain disabled for this host. The
retained overlay must stay enabled while this worker is attached.

## Private request path and authentication

Management creates a CPU runner Job against the shared RWX artifact mount. Its
requests go through `inference.llm-d.svc.cluster.local:8080`, the llm-d endpoint picker,
and the pinned Qwen3-4B model Pod on the A10. The gateway is ClusterIP-only. This
experiment does not establish public DNS/TLS behavior or model-serving HA. Two
Envoy replicas run on separate CPU hosts; one EPP and one GPU model remain single
instances.

`retained_gateway.py` stages immutable API-key Secrets in `llm-d` and `klip`, comparing
existing values rather than rotating them on retry. The local key stays in the private
operator credential directory. Never copy it into a snapshot, command, report or
Helm value. The A10 management overlay mounts only the runner Secret and explicitly
maps its file to the snapshot's `INFERENCE_TOKEN` reference. The generic management
installation retains its ordinary runner command.

The native gateway checks must reject missing/incorrect keys and an unknown model,
and must accept the fixed `gec-small` alias. API-key authentication is sanitized
before forwarding. Ordinary model request-content logging is explicitly disabled
with the flag supported by pinned vLLM 0.32.0: `--no-enable-log-requests`.

## Reproduce the paired run

Use the existing private operator state, verified SSH pins and AWS profile. Do not
run an independent mesh writer from a checkout: the live host executor owns the
peer registry. The retained node's exact UID, boot ID, CUDA proof and cross-node
network evidence are stored in the ignored acceptance directory. Neither staging
nor serving release tools have a provider lifecycle interface.

The two versioned serving profiles under `deploy/environments/lab/experiments/a10/`
share Qwen3-4B/model/tokenizer pins, BF16, non-thinking chat settings, context 768,
eight sequences, output cap 128, 0.8 GPU memory utilization and disabled prefix
caching. Only the second profile enables n-gram prompt lookup: five speculative
tokens, minimum lookup length two and maximum five. This uses no draft model.

Prepare public JFLEG with the pinned tokenizer and the same `enable_thinking: false`
setting. The retained preparation has separate development warmup and test evaluation
records, all four references, actual rendered token counts and checksums. Raw inputs
and outputs stay outside Git. Staging records the source manifest checksum and copies
the exact entries to the shared mount **before** the snapshot is frozen. It does not
rewrite launch inputs.

For each variant, first use `retained_model.py prepare` with the exact native model
scan directory. Inspect its saved rendered chart, then call `deploy --approve-plan`
with the returned checksum. Finish and collect a baseline before changing to n-gram
serving. A saved release attempt cannot automatically replay.

Stage a fresh measured inventory and canonical run, using a new identity:

```sh
uv run --locked --all-packages python scripts/infrastructure/retained_experiment.py \
  --evidence artifacts/acceptance/manual-a10-20261104 \
  --variant baseline --identity a10-baseline-20261104-a

# Wait for that named Kubernetes planning Job to succeed, then submit promptly:
uv run --locked --all-packages python scripts/infrastructure/retained_experiment.py \
  --action submit --evidence artifacts/acceptance/manual-a10-20261104 \
  --identity a10-baseline-20261104-a
```

The submission uses management's durable inventory/external-capacity/prepared-run
imports and actual runner Job. Repeated submission uses the same idempotency key
and observes the same attempt. A stale frozen inventory needs a distinctly named
new plan; do not refresh history in place. External capacity has
`capacity_lifecycle: externally_retained`, `ttl_minutes: null`, and no expiry.
Management lifecycle allocation is explicitly disabled for this A10-only release.

Run the n-gram variant with a new identity after its serving profile is ready. Keep
seed 42, the selected source records, admission offsets, rates, warmup, measurement
phases, timeouts, delivery policy and customer targets unchanged between the pair.
The canonical run has 60 seconds of unscored warmup, 180/60/360 scored seconds and
120 seconds of maximum drain. Short diagnostics are unscored and do not replace it.

## Metrics and interpretation

`monitoring.yaml` places ServiceMonitors/PodMonitors in `observability`, a namespace
watched by the existing operator, and limits scrape access to the exact targets.
The provisioned **KLIP / A10 WriterAssist speculation** dashboard uses observed metric
names from the actual binaries:

- Gateway health, request concurrency/latency and API-key rejection counters. Gateway
  latency selects the `http-8080` listener and excludes admin/readiness/scrape traffic.
- llm-d ready endpoints, scheduler latency, selection errors and model-poll failures.
- vLLM queue/running counts, E2E/queue/TTFT histograms, token throughput and KV usage.
- DCGM utilization, real framebuffer usage, temperature and power for the exact GPU UUID.
- Speculative proposal/acceptance diagnostics are added only after their real series
  are observed on the n-gram deployment; the baseline's missing counters are not zero.

The view is private and authenticated. Dashboard provisioning uses the existing
ConfigMap mount, preserving all earlier dashboards. It requires no Grafana restart.
`retained_metrics.py` uses an operator-only, loopback-bound, short-lived port-forward
to read private Prometheus/Thanos APIs, and always closes the tunnel.

Client records remain authoritative: eligible original requests are the denominator,
failed/rejected/timed-out requests count against attainment, warmup is excluded,
fixed windows require sufficient samples, and invalid generator delivery produces
no SLO assessment. WriterAssist's original target remains 95% completion within one
second, 300-second windows and 100 eligible samples. A completed run may violate it.
Non-streaming requests provide client completion latency, not client TTFT.
JFLEG GLEU and quality coverage are reported independently. One pair is exploratory
calibration evidence, not a production capacity promise.

## Evidence and retained risks

Each run keeps its frozen snapshot, preparation, schedule, raw exports, normalized
records, quality and report. Management verifies/registers result artifacts before
terminal completion. Local downloaded artifacts have verified checksums. Keep
failed attempts and original assessments; re-evaluation creates new artifacts.

The exact model and EPP vulnerability scans are **not clean**. The private experiment
has time-limited, image/configuration-bound applicability reviews in the two
`deploy/retained-a10-*-risk-review.json` files. The model scan includes Critical
kernel-header package advisories and runtime High findings; EPP has one runtime
High finding. The narrow reviews do not establish production readiness or authorize
public exposure. Do not turn a bounded experimental exception into a general
scanner exclusion. Respect expiry and resolve findings before broader deployment.
