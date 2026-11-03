# Platform observability

The `platform-observability` Terraform root owns the external readiness monitor,
notification topic and telemetry storage/identities. Kubernetes telemetry releases
are a separate deployment step. A successful allocation does not prove that
metrics, logs, traces, dashboards or email delivery work.

## Allocate and verify

Use the existing administrator profile and reviewed-plan operator:

```sh
make observability-cost
make observability-plan
make observability-apply PLAN_SHA=<exact-reviewed-plan-sha256>
make observability-verify
```

The authorized email destination lives only in the private operator state's
`cluster/observability/alert-receiver.json`. Its fields include `protocol: email`,
`endpoint`, an explicit UTC `authorized_at`, and the authorization purpose.
The input variable is sensitive. Private Terraform plans/state contain the
destination; do not attach raw plans, state or subscription API responses to
public verification bundles. Never commit subscription confirmation links.

The first subscription sends an AWS confirmation email. The recipient must click
its confirmation link. Creating a subscription cannot confirm on the recipient's
behalf. `make observability-verify` reports only its confirmation state, without
printing the address. Do not recreate an awaiting subscription repeatedly.

After confirmation, send one authorized delivery test:

```sh
make observability-test-notification OPERATION=<new-12-hex-id>
```

An accepted SNS `MessageId` proves submission, not recipient receipt. Obtain a
recipient acknowledgment separately. The operation journal is written before
publishing. Reusing an operation returns its original evidence, even after a
response is lost. It never silently republishes an ambiguous submission.

## Independent availability path

Route 53 checks `https://management.klip-lab.click/health/ready` from three AWS
health-check regions every 30 seconds, with three consecutive failures required.
This endpoint reaches the API/database readiness check through the web proxy.
A CloudWatch alarm uses two one-minute unhealthy samples, and treats missing data
as breaching. Both alarm and recovery notifications use the encrypted SNS topic.
This path runs independently of the cluster and remains useful when Prometheus
or Alertmanager cannot run.

Route 53 health-check metrics, the alarm, SNS topic and its KMS key are explicitly
in `us-east-1` as required by Route 53 monitoring. Applications and all three
telemetry buckets remain in `eu-west-2`. No compute or GPU region changes.
Route 53 HTTPS health checks do **not** validate TLS certificates. The later
blackbox checks must cover certificate trust, hostname and expiry separately.

A manual SNS test proves only delivery. An alarm-state simulation proves only
alarm routing. Retain a real controlled outage and recovery observation before
claiming the full external detection path is live-verified. Avoid manufacturing
an outage while other recovery procedures are running.

## Storage, identity and retention

Three separately scoped S3 buckets store metrics, logs and traces. All are
private, encrypted, versioned and retained across cluster replacement. Terraform
refuses bucket/key destruction. Do not remove these guards during a routine
destroy-and-rebuild. Retain the OIDC issuer/provider and workload role identities
with them; replacement cluster trust must be deliberately verified.

Service accounts in namespace `observability` have separate exact-subject roles:

| Workload | Permission |
| --- | --- |
| `klip-prometheus` | Read/write metrics blocks, no block deletion |
| `klip-thanos-store` | Read metrics only |
| `klip-thanos-compactor` | Compact/expire metrics blocks |
| `klip-loki` | Manage only the logs bucket |
| `klip-tempo` | Manage only the traces bucket |
| `klip-alertmanager` | Publish only the operational topic; use its KMS key only through SNS |

No workload receives `DeleteObjectVersion`, bucket administration, host identity
fallback, static AWS keys or infrastructure mutation privileges. Merely allocating
these roles does not enable Pod credential injection: extend the scoped identity
webhook allowlist during the release step, and test positive and wrong-subject
cases before relying on federation.

Intended logical retention is metrics 14 days, logs 30 days and traces 7 days.
These must be enforced by each engine's provisioned configuration. S3 lifecycle
expires only noncurrent versions after seven days and abandoned multipart uploads;
it does not age out current blocks/indexes independently. Current storage can grow
until an engine's retention is running. Monitor both logical and noncurrent bytes.

At current published rates the root adds an estimated $2.60/month before usage
($1.50 HTTPS health check conservatively excluding discounts, $0.10 alarm,
$1.00 KMS key). 100 GB of London S3 Standard storage is approximately $2.40/month.
Requests, transfer, taxes, retained versions, future KMS rotations and Kubernetes
telemetry volumes are additional. `make observability-cost` records this scope and
the pricing references before applying. It is an estimate, not a spending cap.

## Remaining release and acceptance work

`make observability-charts` fetches the seven checksum-locked upstream archives.
`make observability-render` re-verifies/re-extracts them and renders the monitoring
release using immutable Linux AMD64 images. It validates monitoring custom
resources against the schemas from that exact chart and rejects non-expiring API
tokens, mutable auxiliary images, host networking and missing placement/spreading.
These checks also run in the infrastructure CI workflow.

The first monitoring values reserve two Prometheus instances (500m/2 GiB each,
plus 100m/256 MiB sidecars), three Alertmanager instances (50m/128 MiB each),
one recoverable operator and one recoverable kube-state-metrics exporter. Each
Linux host gets a 25m/64 MiB node exporter on Pod networking, with read-only host
mounts and no host PID namespace. These agents require their own narrowly
privileged namespace for host mounts; do not relax the main observability namespace.
The values request 43 GiB of retained gp3 storage. These are rendered requirements,
not allocated volumes or a complete one-host-loss capacity proof.

Prometheus keeps six hours locally and uploads blocks through Thanos sidecars.
The later Thanos Query release must deduplicate on `prometheus_replica` and the
compactor must enforce the declared 14-day metric retention. Scraping replicas
alone do not establish a deduplicated query path. The initial render disables
private etcd, controller-manager, scheduler and kube-proxy scraping until their
authenticated endpoints are explicitly configured; do not present those views as
healthy in the meantime. Image digests are resolved, but vulnerability review,
network policies, Pod federation, CRD ownership, headroom and server-side checks
remain mandatory before installation. The render command performs no deployment.

Deploy replicated scraping with Thanos deduplication, Alertmanager, PostgreSQL-backed
replicated Grafana, HA monolithic Loki, a recoverable single Tempo backend and
buffered collectors. Tempo 3 distributed mode requires Kafka; the initial small
platform intentionally uses monolithic mode and must not describe it as HA.
Use checksum-pinned community charts and immutable images. Keep ingestion/query
endpoints private and dashboards authenticated. Validate declared CPU/memory
requests and one-worker-loss headroom before deploying.

Provision the twelve agreed diagnostic views using actual exposed metrics.
Missing sources remain No data. Do not derive canonical experiment SLOs from
server histograms: normalized client records remain authoritative. Prompts,
responses and credentials must stay out of ordinary telemetry; request/run IDs
belong in structured correlation fields rather than unrestricted index labels.

Official references:

- [Route 53 monitoring](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/monitoring-health-checks.html)
- [SNS email confirmation](https://docs.aws.amazon.com/sns/latest/dg/sns-email-notifications.html)
- [Encrypted SNS service permissions](https://docs.aws.amazon.com/sns/latest/dg/sns-key-management.html)
- [Loki monolithic mode](https://grafana.com/docs/loki/latest/setup/install/helm/install-monolithic/)
- [Tempo deployment modes](https://grafana.com/docs/tempo/latest/reference-tempo-architecture/deployment-modes/)
