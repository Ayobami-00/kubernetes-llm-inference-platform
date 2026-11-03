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
The live AWS Pricing review on 2026-11-03 reports $0.0928/GB-month for London gp3:
43 GiB adds approximately $3.99/month, excluding snapshots, transfer and tax.
Retention continues billing after workload removal until the explicit storage
cleanup procedure resolves those volumes.

Prometheus keeps six hours locally and uploads blocks through Thanos sidecars.
The later Thanos Query release must deduplicate on `prometheus_replica` and the
compactor must enforce the declared 14-day metric retention. Scraping replicas
alone do not establish a deduplicated query path. The initial render disables
private etcd, controller-manager, scheduler and kube-proxy scraping until their
authenticated endpoints are explicitly configured; do not present those views as
healthy in the meantime. Image digests and a bounded runtime vulnerability review
are recorded. Network policies, live Pod federation, CRD ownership, headroom and
server-side checks remain mandatory before installation. The render command
performs no deployment.

The exact-image scans retain 36 High findings across node exporter,
kube-state-metrics and the patched Thanos 0.42.5 runtime. No Critical findings were
reported. `deploy/observability-risk-review.json` records every High package/version,
the restricted runtime conditions and exact configuration hashes. Its seven-day
expiry blocks new rollouts. It does not claim the dependencies are fixed or that
source reachability analysis proves their safety. Replace with tested upstream
fixes when available; any changed finding, image or runtime boundary needs a new
review. Complete raw scans and source analysis are retained in the linked CI runs.

Install the versioned namespace/network boundary first, then update the exact
workload-identity webhook allowlist:

```sh
make platform-service-deploy SERVICE=klip-observability
make platform-service-deploy SERVICE=klip-workload-identity
```

These commands do not install the monitoring applications. The boundary keeps
metrics private, disables exporter access to Secrets/ConfigMaps and separates
read-only host mounts from the restricted application namespace. Node exporter
uses Pod networking; namespace-dependent network collectors are disabled rather
than presenting Pod counters as host statistics. Use the host instrumentation
and Cilium/Hubble sources for the actual host/network view.

Prepare the application release using the directory containing the exact reviewed
CI `summary.json` and raw image reports, then apply its saved identity:

```sh
make monitoring-prepare SECURITY_DIRECTORY=<downloaded-reviewed-scan-directory>
make monitoring-apply PLAN_SHA=<exact-prepared-monitoring-sha256>
make monitoring-observe
```

Preparation verifies actual worker capacity, deployed policies/admission, live IAM
trust/inline-policy equality and complete CRD ownership. Apply rechecks these
conditions, verifies the plan digest and source, and re-extracts checked archives.
The ten operator CRDs have a separate retained owner; existing unowned schemas
are not adopted or overwritten. New versions require explicit schema review.
Server-side validation precedes the atomic Helm application release. An
interrupted install is retained for inspection and never automatically replayed
under its original identity. Replica readiness is an installation observation;
scrape/ingestion/query, denied-access and retention/failure acceptance are separate.

Cross-host kubelet scrapes use the existing WireGuard path. Cilium masquerades
those requests to the source platform worker's WireGuard address. The initial
host firewall permitted control-plane kubelet clients but blocked those CPU peer
addresses. `make monitoring-firewall` permits only the three reserved platform
CPU `/32` sources, on `wg0`, to TCP 10250. It preserves API, etcd, SSH, metadata
isolation and all other rules. Kubernetes token/RBAC authentication remains
required, and the ServiceMonitor verifies the serving certificate against the
private Kubernetes CA. No `nodes/proxy` privilege is added.

The Ansible update checks host identity and prior file hashes, syntax-checks the
rules, applies one host at a time, then verifies the original Node UID, three
healthy etcd members and all database replicas. A drifted firewall fails before
copying. The same sources are included in generated bootstrap rules for fresh
and subsequent GPU hosts.

On a fresh installation, the upstream chart initially ignores admission failures
while its serving Pod/certificate comes up. After readiness the operator performs
an unchanged Helm upgrade and requires both monitoring admission configurations
to use `Fail` with an injected CA. Original release plans, values, logs and terminal
observations are archived with hashes before the next release is prepared.

Server dry-run uses merge-patch validation to match Helm's ownership semantics;
it does not claim server-side-apply ownership or force live field adoption. If
this validation fails before Helm is invoked, inspect the original trace and
`make monitoring-observe`. With the existing release Ready and no pending Helm
transition, `make monitoring-reconcile PLAN_SHA=<original-sha>` archives that
failed pre-Helm attempt without changing workloads. Prepare a new corrected plan.
An interrupted Helm install/upgrade remains ambiguous and this command refuses
to retire it or replay the transition automatically.
A completed Helm command followed by an admission postcondition failure can be
retired only after observing a Ready release, no pending Helm transition and a
deployed Helm revision with exactly the attempted values. It remains a failed
acceptance record; original values/logs are archived and a corrected plan gets a
new identity. The chart's `IgnoreOnInstallOnly` setting is explicit because an
empty failure-policy value with cert-manager otherwise remains `Ignore` on upgrade.

The separate Thanos release has two spread query replicas and two spread store
gateways, plus one recoverable compactor. Query deduplicates on
`prometheus_replica`; the compactor enforces 14 days at every resolution. Receive,
Ruler, query frontend and embedded object storage are disabled. Store uses the
read-only metrics role, compactor uses the scoped compaction role, and query has
no AWS identity. The additional 20 GiB of retained gp3 is about $1.86/month at the
same reviewed London price. Five containers request 500m CPU and 2 GiB total.

Update the boundary chart to include its private query/store policies, then use
`make thanos-render`, `make thanos-prepare SECURITY_DIRECTORY=...` and
`make thanos-apply PLAN_SHA=...`. The plan freezes values, image review and actual
host identities. Completion checks the 2/2/1 workload topology; verify both hot
replicas, query deduplication and actual object upload separately. Prometheus uses
the standard two-hour blocks, so an empty initial bucket is not an upload proof.

Thanos query defaults to failing a query when a store response is incomplete;
dashboards must not silently treat partial responses as complete data. Its
StatefulSet Pod security contexts are explicit because the upstream component
defaults override global settings. Static validation checks every application
and init container, and server dry-runs validate Pod specs/PSS before Helm.
The Pod dry-run uses the existing default ServiceAccount because Helm has not
created the component accounts yet; actual federation remains a separate live
postcondition.

If a first atomic Thanos install rolls back, `make thanos-reconcile PLAN_SHA=...`
requires the release Secrets and all its workloads to be absent. It records the
attempt as failed and preserves verified retained-volume identities; it never
reruns the failed transition. An existing or pending Helm revision is ambiguous
and rejected. A corrected install requires a new source-bound plan.

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

## Dashboard, log, trace and ingestion releases

The versioned component values use two Grafana replicas, three monolithic Loki
replicas with replication factor three, one recoverable Tempo instance, and two
OpenTelemetry Collectors with independent persistent queues. Loki retains 30 days
and Tempo seven days. This adds 50 GiB of retained gp3 (about $4.64/month at the
reviewed London rate), plus object-storage, requests and transfer usage. Grafana
metadata uses an isolated `grafana` database/role on the existing CNPG cluster;
it has no separate SQLite volume. Its shared encryption key and credentials are
retained on the operator machine outside the cluster, with an age-encrypted local
recovery copy and a verified independent age-key copy. The private deployment
uses `telemetry_secrets.py stage-local`; it makes no cloud upload. `verify-local`
and `restore-local` validate the exact ciphertext and refuse to overwrite a
different identity. Operator disk loss is not covered by this local copy.

Automatic approval review rejected uploading the newly generated Grafana
credentials to `klip-database-127811062476-eu-west-2`, despite verified private
account ownership, encryption and recovery requirements. The cloud backup
procedure remains implemented but requires separate explicit user approval for
that payload/destination before invocation. It is not part of automatic Grafana
installation. Do not claim independent remote credential recovery until it passes.

Use the exact candidate scan artifacts from workflow `41731963265` to prepare
these releases. `deploy/telemetry-risk-review.json` records the still-unfixed
findings: eight unique High tuples across Grafana's bundled plugins (39 raw
findings), one in Tempo and one in the Collector. The restricted configuration
is part of the review. Grafana has no public endpoint in this release; public
exposure requires a new boundary review and HTTPS validation. Unused datasource
plugins, anonymous access, signup, plugin downloads and public egress are disabled.
These restrictions mitigate exposure, without claiming vulnerable code is fixed.

```sh
make telemetry-prepare COMPONENT=loki SECURITY_DIRECTORY=<exact-scan-directory>
make telemetry-apply COMPONENT=loki PLAN_SHA=<saved-plan-sha256>
make telemetry-observe COMPONENT=loki
```

Repeat for `tempo`, `otel` and `grafana`. Prepare/apply checks fresh identities,
Pod security admission, retained claim ownership and live resource reservations.
It reserves one additional 1-CPU/2-GiB runner and requires 10% request headroom
against the reviewed single-worker-loss envelope. This is resource accounting,
not a scheduler or unplanned-failure proof. It does not authorize unlimited runs.
Each component retains its own plan/history; an interrupted transition is never
replayed automatically.

`make telemetry-validate` uses the immutable upstream Loki, Tempo and Collector
binaries to check rendered configuration in credential-free Docker containers
with networking disabled. CI runs the same check and retains its logs. This is
configuration validation, not an ingestion or storage acceptance test.

The Collector uses memory limiting, batches capped at 512 records, 1,000-request
persistent queues per exporter and retries capped at five minutes. Each replica
has 5 GiB queue storage. Queue exhaustion, permanent export errors, disk exhaustion
and upstream application drops must be alerted and reported; buffering is not a
claim of lossless telemetry. Only scoped application/runner/serving/agent Pods
may ingest. Logs and traces remain private; Grafana has fixed internal data
sources. Prompts, responses and credentials must not enter ordinary telemetry.

A separate headless `klip-thanos-stores` Service discovers both store replicas
individually without renaming their StatefulSet or replacing retained claims.
It is owned by the boundary chart; the upstream Service remains unchanged.
