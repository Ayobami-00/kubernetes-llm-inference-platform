# End-to-end platform delivery

Current evidence summary as of **5 November 2026**. The reference platform has
recorded CPU, management and single-A10 serving evidence. Its AWS environment was
then [decommissioned](../runbooks/full-project-decommission.md); hosted endpoints
are offline. The broader roadmap still includes uncompleted rebuild, expansion
and sustained-failure gates.

## Current acceptance scope

| Area | Recorded evidence | Remaining boundary |
| --- | --- | --- |
| Persistent platform | Three control planes, three CPU workers, HA management, CloudNativePG promotion/isolated restore and durable artifact access | Full destructive destroy/rebuild acceptance remains unproven |
| Provider lifecycle | Guarded adapters, prerequisite receipts, restricted executor and durable provider intents | The measured GPU was manually retained; automatic end-to-end GPU provisioning is not inferred |
| GPU runtime | The A10 joined and exposed one GPU; discovery, plugin/CUDA checks and DCGM observations | This host kept its existing driver/toolkit; full installer ownership was not tested |
| Serving | Pinned Qwen3-4B through private authenticated llm-d routing and verified effective gateway timeouts | Public inference DNS/HTTPS and redundant model-serving capacity were not benchmarked |
| WriterAssist | Canonical historical pair and the real dashboard-driven 220-request-per-variant comparison, with valid delivery, GLEU and SLO assessments | One baseline-first A10 comparison is not broad hardware/customer calibration |
| Observability | Private metrics/logs/traces, dashboards, GPU/server diagnostics, scoped identity/storage and acknowledged alert delivery | Historical acceptance is not a live-health or production-certification claim |
| Run recovery | Management API/worker replacement preserved the same runner Job/snapshot/clock; cancellation retained partial evidence | Runner loss, sustained serving/node failures and full failure campaigns remain separate tests |
| Expansion | All five public preparation/local execution paths and seven synthetic scenarios | Other real-model profiles and mixed-GPU capacity remain outside the A10 experiment |

See the [measured dashboard comparison](../experiments/writer-assist-a10-dashboard-2026-11-04.md),
[experiment methods](../../experiments/writer-assist-a10/ngram-comparison.md) and
[operator runbook](../runbooks/dashboard-a10-comparison.md). Retained image findings
and expiring reviews remain explicit. Earlier acceptance records are preserved.

## Implementation history: 3–4 November 2026

The entries below describe the then-current state in delivery order. Statements
such as “no GPU allocated” or “live gate pending” belong to their historical step;
use the current scope above for the latest recorded acceptance.

The additional CPU allocation is defined in a separate `platform` Terraform root;
see the [worker allocation runbook](../runbooks/platform-workers.md). Its reviewed live
plan was applied on 3 November 2026: three CPU workers, no control-plane mutations.
All three passed trusted preparation, Kubernetes/Cilium readiness, kubelet certificate,
route and resource-reservation checks. They carry placement/AZ labels and their bootstrap
taints are removed. This does not establish storage or application HA. The Lambda
credential is configured at the existing ignored secret path and read-only API checks
confirmed A10 capacity and a plain Ubuntu image. Those observations are not reservations.
The automatic-only regional IP increase to eight was approved; no support case was
created. Immediately before application, the operator verified three existing plus
three additional IPs against eight, and six existing plus twelve additional vCPUs
against 32. No GPU has been allocated.

Start with one A10. Shared/CodeAgent testing later uses one 8×A100 **80 GB** VM
sequentially; simultaneous mixed scenarios require additional authorized capacity. A single
GPU host has expected inference downtime during host failure. Experimental SLO violations
remain valid outcomes when delivery and measurements are valid.

## Foundation implementation

`deploy/environments/lab/management.yaml` enables two replicas for web/API/workers,
required host anti-affinity, zone spreading and disruption budgets. It removes the old
control-plane placement key explicitly because Helm merges maps. Provide verified image
digests, existing Secret names, HTTPS origin and RWX storage separately.
Updates replace at most one replica without a surge, so strict host anti-affinity does not
deadlock a rollout when every eligible host already has a replica. A healthy replica must
remain available; single-fault and rollout behavior still require live verification.

The API, workers and migration Job share verified database TLS configuration. Connection
pool health checks remain enabled. The CA must be mounted and conflicting URL settings
fail closed without printing the URL.

Runner CPU placement and tolerations are frozen in new execution bindings. The HA overlay
targets platform workers; historical bindings without these fields retain their original
control-plane placement. Resource/placement changes cannot rewrite an existing attempt.

`charts/klip-database` provides the CloudNativePG resource definitions. See its README for
durability, restore and identity prerequisites. `deploy/platform-services.lock.json` pins
upstream CRD checksums and records schema compatibility; it does not claim installation.

The existing AWS account/region/environment are discovered from `infra/terraform/project.json`.
Before paid expansion, record the approved resource/spending envelope, GPU region/lease,
registry, DNS integration and rebuild target. Keep the existing private API and credential
boundaries. The management backend must not receive the foundation write identity.

## Evidence and remaining work

The initial [availability policy](../architecture/platform-availability.md) records
recovery objectives, durability tradeoffs and the retained-state rebuild boundary.
Its targets remain unproven until the corresponding live tests pass.

Keep live verification bundles under ignored artifact/operational storage, containing
source commit, frozen inputs, pins, preparation hashes, actual inventory, schedule,
request records, exports, quality/SLO reports, telemetry links, interventions and costs.
Keep secrets and raw datasets outside Git.

The initial baseline on 3 November 2026 passed 373 root Python tests (18 optional skips),
17 frontend tests, Ruff/formatting, generated schemas and the production web build. A
read-only AWS query confirmed the three existing control-plane instances running across
London's three configured AZs. These observations do not establish application or GPU
readiness. Live gates remain pending.

The foundation implementation subsequently passed 414 root Python tests, 71 management
database tests, four new Terraform provider-mock cases, TFLint for both new Terraform
units, Helm/custom-resource validation, Ruff/formatting and generated schema/OpenAPI
checks. The earlier frontend checks remain unchanged and passing. Eighteen root tests
are optional skips; these checks do not claim the full CPU integration or live gates.

The worker rollout passed 442 root Python tests (17 optional skips), Ruff/formatting,
Ansible checks and static Helm rendering. Cilium now tolerates the worker bootstrap
taint and rolls at most one agent at a time. Its private API endpoint is generated
from validated foundation allocation, avoiding a startup dependency on Service routing.

One unjoined worker initially missed SSM enrollment during IAM credential propagation.
Authenticated EC2 console evidence identified the failure. A recorded, one-time reboot
was restricted to that exact unjoined instance after checking Kubernetes/mesh absence;
it then registered and completed the normal checks. Encode bounded startup-enrollment
recovery in the reproducible provisioning path before declaring the foundation gate
complete. No account-wide SSM management role or broader SSH rule was enabled.

Fresh live checks subsequently confirmed all six nodes Ready, the original three
healthy etcd members, and foundation Pod-network/Hubble checks. A separate real-Pod
verification passed Service/DNS access, all nine worker-to-worker Pod connections,
metadata isolation and effective MTUs (interface 1420 / route 1370) on the three new
workers. Its isolated test namespace was deleted and cleanup confirmed. The final
Cilium release includes the generated private API endpoint. Operator tunnels use
dedicated, pinned SSH-over-SSM sessions, retain Kubernetes CA/SNI verification and
bind loopback only. The final local Python run passed 448 tests with 17 optional skips.

The retained [workload identity foundation](../runbooks/workload-identity.md) was then
applied through an exact, create-only administrator-owned plan. Public HTTPS discovery
and signing keys at `https://oidc.klip-lab.click` match the verified public signer on
all three control planes, and the IAM provider is restricted to the STS audience.
Its origin bucket remains private and contains only two public metadata objects.
No workload permissions are granted by this publication alone.

After explicit user approval and browser-session renewal, the key-preserving API issuer
transition completed on all three control planes. Both phases checked API readiness,
three healthy etcd members and legacy-token compatibility. New-issuer tokens validated
on every API server; an STS-audience token could not authenticate to the API audience.
Kubeadm's stored configuration and repository bootstrap settings agree. Canary cleanup
was confirmed, and private per-node recovery copies remain retained.

The metadata-only IAM canary role was applied through a separate two-create plan.
Six real projected-token/AWS SDK cases passed: success and scoped S3 reads on every
platform worker, bucket-list denial, and wrong service-account/namespace/audience
rejection. The actual role policies were checked before credentials were projected.
The pinned AWS CLI image ran non-root with a read-only root filesystem and no static
or metadata-fallback credentials. Probe namespaces were deleted and cleanup confirmed.

CSI/backup roles, supported token injection, durable storage, sustained credential
renewal and database rollout remain subsequent steps. Local verification passed
473 Python tests (17 optional skips), the identity root's two provider-mock cases,
Terraform validation, TFLint, all 19 generated schemas and Ruff/formatting.
The workspace declares the public-key verification dependency explicitly.


The committed-code identity repeat passed all six SDK cases; live manifests and
kubeadm configuration agree and an issuer replay would make no changes. Protected
platform placement is enabled on the three recorded CPU UIDs. Each kubelet's real
identity was denied a dry-run attempt to self-assign a protected label. The protected
selector is now used by the HA application/database overlays and identity probes.


Retained storage and its separate connectivity state were applied from create-only
saved plans. Live read-back verified regional EFS encryption, non-root access-point
ownership, TLS/path/root denials, S3 versioning/encryption/recovery retention and exact
workload role policies. All three workers reached their local-AZ mount target over
TCP 2049. No GPU or new compute was allocated. CSI mounts, successful backup recovery
points, database archiving/restoration and sustained identity renewal are still unproven.
The example storage budget is documented in the [storage runbook](../runbooks/platform-storage.md).
Database initialization/recovery templates validate against the pinned CRDs in both
Secret and projected-token modes. Local verification passed 479 Python tests (17
optional skips), four new Terraform mock cases, both storage lint checks, all 19
schemas and Ruff/formatting. Application/database rollout remains pending.


cert-manager 1.21.3 is deployed with six Ready Pods (two per component) spread over
protected CPU hosts. The upstream AWS token webhook 0.6.19 has two replicas and a
cert-manager-issued certificate, with fail-closed admission limited to explicit
storage/database/test accounts. Six injection/SDK cases passed, including all three
workers and wrong identity/audience denials; test namespaces were removed. Credential
renewal over a token/session expiry remains pending.

The pinned EFS CSI 3.5.4 node plugin registered on all three CPU workers. Terraform
continues to own its static access point; the unused controller and its RBAC are
excluded through a bounded Helm post-renderer. Tests use Helm's real post-rendering
path, including the non-hook CSI registration setting. The retained `klip-artifacts`
claim is Bound with TLS/IAM and CPU-only affinity. Seven real canary Jobs passed:
three non-root writes, three readers checking every worker's file, and exact canary
data cleanup. Job cleanup used API-side UID preconditions and was confirmed.
This establishes mounted RWX behavior, not database backup/restore or service failover.
The next dependencies are topology-aware EBS CSI, CloudNativePG/Barman deployment,
real archive/restore and management rollout. No Lambda GPU has been allocated.


The add-on/mount checkpoint passes 494 root Python tests (17 optional skips), all
19 generated schemas, both database bootstrap/identity render paths and Ruff/formatting.
Infrastructure CI fetches checksum-locked service charts before renderer tests, without
cloud credentials. Source fingerprints now include local Helm templates as well as
Terraform, Ansible, deployment configuration and operator code.


EBS CSI 1.67.2 is now deployed with two spread controllers and three CPU node plugins.
The exact projected-token role was applied in a two-create plan, with no host privilege
expansion. Kubernetes metadata labels were reconciled from authenticated AWS identities;
IMDS restrictions remain enabled. Real tests provisioned one encrypted 1-GiB volume in
each AZ, passed three writes and three reads after complete detach, verified Retain
following claim deletion, then confirmed deletion of only the synthetic volumes through
CSI. The owned namespace/PVs and all three AWS volumes are gone; cleanup is complete.
See the [EBS runbook](../runbooks/ebs-storage.md). Database deployment/restore and
management HA remain pending. No GPU has been rented.

CloudNativePG 1.30.1 / Barman 0.15.1 and PostgreSQL 18.6 standard Trixie are now
deployed with pinned charts/images. The three database instances are Ready on
distinct CPU hosts, using independent 40-GiB data and 20-GiB WAL volumes. The
Barman archive safety probe required bucket-level ListBucket on the dedicated
archive bucket; object access remains restricted to `postgresql/`. Actual WAL
archiving recovered, retaining the earlier failure counters honestly.

A real external base backup completed, followed by a separately committed and
archived WAL marker. Isolated recovery to that named point produced three Ready
instances and matching base/WAL markers in about 117 seconds. Exact UID/claim/disk
cleanup removed all six temporary disks; the source database and archives remain.
The application credential was age-encrypted into a versioned off-cluster object
and download/decrypt checked using the existing independently retained recovery
identity. See the [database runbook](../runbooks/management-postgresql.md).
The pinned plugin manager is a single recoverable replica, not HA. Management data
migration, client reconnection/failover and the retained-state environment rebuild
remain unproven. Offline checks pass 527 tests (17 optional skips), all 19 schemas,
strict initialization/recovery CRD rendering and Ruff/formatting.

The preserved local management database has subsequently been copied through a
consistent exported snapshot: all 15 tables and 204 rows matched. C's existing
Alembic revision 0002 then ran using the real application connector over verified
TLS 1.3 as a non-superuser; original row content remained intact. The original local
database/volume are retained. Application deployment and failover tests remain
pending; this migration alone does not pass the management HA gate.

Fresh EBS operations also passed beyond the unchanged controller's original
one-hour maximum credential lifetime. The leader and container remained unchanged,
all temporary disks were deleted, and a successful scoped STS event was observed.
This proves continued storage operations after credential renewal for that tested
controller; other SDK paths and broader availability still need their own evidence.

A retained private image registry is now applied and its live policies verified.
The pinned upstream ECR helper passed its own tests/build on an existing bounded
CPU Job, with verified export and cleanup. [Private image delivery](../runbooks/private-images.md)
still needs host rollout, actual pulls and published management/runner images.

The complete local infrastructure check now passes: Terraform validation/mock
plans and linting, Ansible/Helm checks, 538 Python tests (17 optional skips), all
19 schemas and Ruff/formatting. Private-branch CI exposed and now has fixes for
OCI cache naming, the Kind checksum download and recursive Terraform formatting.
Combined CI is being repeated; local success is not a substitute for that result.

The registry's CI handoff has now been applied and read back: one retained GitHub
OIDC provider, exact immutable repository/branch trust and only scoped image
publishing/metadata permissions. Hosted CI builds private digest-addressed images
and retains scan receipts; it does not automatically deploy them. The local suite
passes 542 tests (17 optional skips) and the frontend builds with 17 passing tests.
Combined CI additionally exposed a historical/current graph canvas lifecycle issue
and a hosted-Linux test-user mismatch reading private runner artifacts. The graph
now uses separate canvas lifetimes and never saves historical layout preferences;
the integration harness reads artifacts through the image's actual non-root user.
Those real browser/Job checks must pass again before application deployment.

All four private CI images were published with digest/scan receipts. Their first
base-image scans contained critical findings, so application deployment is held
while maintained pinned replacements are built and rescanned. No latency/SLO or
production-readiness claim follows from publication. The upstream Pod-bound ECR
helper has separately been installed on all three CPU workers through the encoded,
reviewed rolling Ansible workflow. Each host's original kubeadm/configuration
hashes were preserved, node UIDs remained unchanged and database/node health checks
passed between updates. Private-pull authentication/denial acceptance and actual
management application rollout remain outstanding. Offline checks now pass 548
tests (17 optional skips); no GPU has been allocated.

The complete offline infrastructure command also passed with those 548 tests,
Ansible/Helm validation, Terraform validation/mock plans/lint and all 19 schemas.
The namespace-only pull-audience release is installed. At source `9567a39`, private
publication, contract CI, infrastructure CI and every management CI job (including
the previously failing browser navigation) are green. Actual runner-Job integration
CI is still running; patched-base rebuilds require their own results.

The remaining `9567a39` workload CI has now passed too: all seven actual AIPerf
scenario paths completed through management in Kind, alongside container/CPU
integration checks. Live private-pull acceptance also passed five checks (three
worker pulls and two wrong-identity denials), with complete UID-bound cleanup.
The patched Python backend scan has zero critical findings but eight high findings
to review. The full newer Nginx image still had vulnerable unused packages, so the
frontend now selects its pinned upstream slim variant pending a fresh scan and
runtime/browser checks. Management application deployment and HA acceptance remain
outstanding; none of these isolated checks are live inference/SLO evidence.

The subsequent slim-runtime scan has zero critical findings in all four images;
the remaining high findings have a package/version-specific applicability review
with a 2026-12-03 deadline. They remain visible. New/unreviewed findings, incomplete
coverage, stale scans or weaker runtime controls prevent deployment. The review
excludes arbitrary scripts and workstation preparation.

A further layer-toggle browser failure was traced to discarding React Flow's
controlled-node measurements during layout. The corrected update preserves node
state, with a repeated layer-toggle browser regression. Backend runtime Pods and
runner Jobs now use RuntimeDefault seccomp and fixed C locale settings. The lab
management chart supports internal HTTPS and cert-manager renewal through Nginx's
native variable-certificate cache, without adding a watcher or API privileges.

Management release preparation/deployment is encoded with source/image checks,
node/namespace/Secret identity guards and atomic Helm deployment. The previously
absent admin credential was initialized once after checking all worktrees and the
live namespace; no service/auth Secret or active legacy session existed. Its value
stays in the ignored private file. Three immutable application/CA Secrets are now
staged, and the dedicated namespace enforces restricted Pods. No worker was started
and no GPU rented by staging. See [management deployment](../runbooks/management-deployment.md).
Local verification passes 560 root tests (17 optional skips), 71 backend tests in
a disposable database and 17 frontend tests/build/format checks, with current
schemas/OpenAPI. Live application deployment, TLS/renewal and HA failure acceptance
still require their own evidence.

The reviewed release has now been applied (2026-11-03 19:03:42 UTC). Live check
`6a5d3214f09c` passed: six Ready application Pods, required per-role host spreading,
actual TLS 1.3 PostgreSQL connections from both API Pods as the non-superuser role,
authenticated internal HTTPS with Secure cookies, 21 retained run records matching
the database, and shared artifact reads from all four backend Pods. The canary is
removed. Matching image scans and management CI passed at source `f39cc4f`.
An encoded allowlisted evidence bundle retains hashes and excludes actual secrets.
Local checks now include 562 passing root tests (17 optional skips). The private
deployment is not an external-frontend or failure-recovery proof. Certificate
rotation, PostgreSQL/application failover, monitoring, live GPU lifecycle and
destroy/rebuild acceptance remain required. No Lambda GPU has been allocated.

The dedicated three-zone management ALB, ACM certificate and two-rule WAF are now
applied from an encoded, saved 17-create Terraform plan. Public management DNS is
still unpublished. The current estimate is $43.40/month at one average LCU, before
request/transfer/tax charges. The next application release adds the HTTPS NodePort,
bounded proxy trust and API ingress policy; fresh three-target health and verified
authenticated HTTPS are mandatory before DNS activation.

Forced cert-manager renewal `57ccd657bb0b` passed at 2026-11-03 19:27:00 UTC on the
initial release: leaf revision 1 became 2, its public key changed, the CA remained
unchanged and both exact web Pods served the new certificate without a Pod/container
restart. Authenticated HTTPS still returned all 21 retained runs. This proves the
tested issuance/mount/cache path, not a full calendar renewal interval or ACM
renewal. Local checks now pass 576 root tests (17 optional skips), with current
schemas and clean Ruff/formatting. Failure recovery and GPU gates remain pending.

The public frontend release is deployed and DNS active at
`https://management.klip-lab.click`. Its three exact targets are healthy, the
two-rule WAF is attached, and the actual public browser authenticated, displayed
the topology/inspector and all 21 historical runs with zero browser errors and
Secure/HttpOnly cookies. Deployment evidence `965990a269cb` confirms the new
release's database TLS, spreading and shared artifacts. Matching source `e347fc0`
passed image publication, contracts, infrastructure, management browser/API checks
and all seven actual-runner scenario paths in Kind.

Single API Pod fault `3b562aa6e841` passed: 20/20 authenticated probes were HTTP 200,
the exact old Pod UID was replaced, and two spread Ready replicas returned about
10.8 seconds after intervention. A subsequent UI read preserved all 21 histories.
Terminal operation reuse preserved the original proof checksum. The test uses
one-second samples and graceful Pod deletion; it does not establish worker
ownership during a run, PostgreSQL failover, CPU loss or GPU resilience. Offline
checks pass 592 tests (17 optional skips). The authorized alert receiver reference
is retained privately; notification delivery remains unverified. No GPU is rented.

Database-primary acceptance is now live-verified. A fresh external base backup and
post-backup WAL marker (`a6e23d591fc0`) completed before testing. The first fault
(`f1d820c7369a`) exposed the stock 180-second smart-shutdown wait with pooled
connections: automatic promotion took about 184 seconds. The failed observer and
later data/timeline reconciliation are retained separately; its timer was not reset.

The chart now sets a five-second smart-shutdown window. A reviewed Helm upgrade
proved that was its only manifest change, leaving all Pod identities, storage,
images and synchronous quorum unchanged. The repeated fault (`c1a7f309e684`)
restored committed new-primary writes in 36.043 seconds and the same authenticated
application session within a conservative 43.080 seconds. All 21 histories and
both markers survived; three Ready replicas and WAL on timeline 3 were verified.
Its 29 failed probes remain visible. The required 120-second objective was retained.

Local verification passes 615 tests (17 optional skips), with current schemas and
clean formatting/lint. External alerts and the telemetry stack are next, before
CPU-host loss, so subsequent debugging retains logs beyond Pod deletion. Management
worker ownership during a real runner Job, GPU lifecycle/inference and the complete
retained-state rebuild still remain required. No GPU has been allocated.

The retained telemetry foundation is now applied (2026-11-03). Its reviewed
41-create plan partially succeeded, but SNS rejected wildcard actions in a topic
resource policy. The original plan/error were retained; explicit supported SNS
actions were tested and a reviewed three-create repair plan completed. The live
metrics/logs/traces buckets are encrypted, versioned and public-access blocked.
Six exact-subject workload roles and an independently hosted management-readiness
alarm are allocated. The external alarm has accumulated healthy samples and is OK.
The fixed estimate is $2.60/month plus storage/usage, excluding Kubernetes telemetry
volumes. Notification confirmation/delivery must be verified separately; no email
receipt or actual outage detection is claimed by allocation evidence.

Seven upstream chart archives and the initial monitoring images are checksum/digest
locked. The first monitoring configuration renders 72 objects and validates against
the pinned Prometheus Operator schemas, using projected API credentials rather
than the chart's non-expiring token default. It is not installed. Image-security
review, scoped federation, network policies, query deduplication, remaining
telemetry backends/views and failure headroom still precede live acceptance.

The combined local infrastructure procedure passes: Terraform validation/mock
tests/lint, Ansible production-profile lint, static Helm checks, monitoring schema
validation, 632 Python tests (17 optional skips), Ruff and all 19 current schemas.
The user reported email confirmation, but the fresh AWS query still reports
`PendingConfirmation`; the delivery-test command correctly stopped before
publishing. The unsent test identity is `6ab3c19d8f20`. It may be retried only
after AWS confirms the destination; no receipt or alert-delivery pass is recorded.

The subscription is now confirmed in AWS. The one delivery test `6ab3c19d8f20`
was accepted at 20:38:56 UTC and the recipient explicitly acknowledged receipt;
the acknowledgment was retained at 20:44:40 UTC. This proves the tested SNS email
path only, not Alertmanager workload delivery or actual outage detection.

Live request accounting including native sidecars found 4.9 CPU/8.19 GiB already
reserved on the platform workers. Full telemetry plus one runner forecasts 9.86
CPU, exceeding the old seven-CPU single-worker-loss capacity. A versioned upgrade
to three m7i.2xlarge workers is prepared, with at least 15 CPU/58 GiB remaining
after one loss. Its incremental compute estimate is $510.49/month, within the
authorized persistent tier envelope. At that planning stage no worker had been resized.

The scoped CPU-options deployer permission is applied from a reviewed one-policy
plan; an actual worker dry run succeeded and the authoritative control-plane dry
run was denied. Fresh backup `d1f7a82c4e90` and post-backup WAL were verified at
20:55:22 UTC. The maintenance workflow preserves per-node allocation truth during
rollout, respects PDBs and drains the selected ALB target before restart. Planned
maintenance will not replace the separate unplanned host-loss test.

The rolling upgrade completed at 21:31:09 UTC. Each exact worker retained its
instance, Kubernetes UID, root disk, network identity and placement, and exposes
eight CPUs with 7.5 CPU and about 29.07 GiB allocatable. Workers 01 and 03 completed
first. The first worker-02 drain stopped before any EC2 mutation because ALB
health had not converged after rescheduling; that failed receipt was retained,
scheduling restored explicitly and all three database/target checks revalidated.
A tested bounded health-observation wait now precedes target deregistration;
identity errors and the two-healthy-target requirement remain unchanged.

Post-maintenance check `1483b7bc92c1` passed management spreading, database TLS,
all 21 historical runs and shared artifacts across four backend Pods. The public
HTTPS frontend and all three exact ALB targets passed at 21:32:36 UTC. Fresh
backup `8c49fd62a731` completed with backup ID `20261103T213229` and post-backup
WAL `0000000400000000000000B7` on timeline 4. These are planned-maintenance proofs;
the unplanned CPU-loss and restored-cluster gates remain separate.

The full local suite then passed 646 tests (17 optional skips) and clean Ruff/
format checks. Monitoring-image scan `41722600867` and exact exporter source
analysis are retained as security evidence. No Critical findings were reported;
High findings require individual runtime review before monitoring installation.
The Thanos 0.42.5 candidate reduces the original scan's High count from 23 to 11;
its image is not authorized merely by this scan. No GPU has been allocated.

The private monitoring core installed on 2026-11-03 and admission was verified
fail-closed at 22:18:51 UTC. Both Prometheus replicas subsequently scraped all
43 initial targets successfully. Kubelet scraping verifies serving certificates
against the Kubernetes CA; the host firewall permits only the three reserved
platform-worker WireGuard addresses on TCP 10250. The update ran sequentially
across all six CPU hosts, preserving node identities, etcd health and database
replicas. Controller-manager, scheduler, etcd and kube-proxy metrics remain
disabled until authenticated endpoints are configured.

The exact monitoring-image review accepts 36 individually recorded High findings
under restricted runtime conditions, with no Critical findings and a seven-day
expiry. These vulnerabilities remain outstanding. Local checks passed 670 tests
(17 optional skips), Ruff and formatting. The preceding combined CI baseline at
`1b5424e` passed all four application/infrastructure workflows.

Thanos plan `f5d777b637bb4805c0ebe377e230c5cf6176e9dd810ee4a73575cada1e997014`
started its atomic install at 22:34 UTC. Query replicas started; Kubernetes rejected
the store/compactor Pods because upstream component defaults override the global
seccomp setting. This is a failed acceptance, not a usable retained-metrics path.
The installation must roll back before a corrected, separately identified plan.
No GPU is rented while this prerequisite work proceeds.

Thanos subsequently installed at 22:48:15 UTC. The discovery update
`08af4354d254d5be1b44d9b21dee558c80a1eb2f4e8640abbb0afba5ef6a8d3f`
completed at 22:59:01 UTC without replacing the store StatefulSet or claims.
Live queries discovered both hot sidecars and both store gateways with no errors;
API-server series count was three with deduplication and six without it. Object
upload and retained-history query remain separate acceptance checks.

The remaining upstream configurations passed native image validation without
network/credentials in CI `41735004751`. Loki's three monolithic replicas became
Ready at 23:07:55 UTC; singleton Tempo at 23:11:01 UTC; both Grafana replicas at
23:16:36 UTC; both persistent-queue Collectors at 23:18:50 UTC. These installation
observations do not establish telemetry HA or retention by themselves.

Grafana proof `b4e8f209c36d` verified two hosts, database health, the three shared
datasource identities and deduplicated queries through each replica. A synthetic
annotation written through one replica was visible through the other and then
removed. Ingestion proof `ae7c4382f950` sent one explicitly synthetic log/trace
from a management Pod; both returned HTTP 200 and were retrieved through Grafana.
No customer content, prompts or credentials were emitted by the probes. Actual
application SDK instrumentation, host/GPU sources, full diagnostic views, public
HTTPS and controlled telemetry failures remain pending.

Automatic approval review rejected uploading newly generated Grafana credentials
to the private project recovery bucket, even after ownership/encryption checks.
That upload has not occurred. The safer private rollout uses a verified local
age-encrypted recovery copy and leaves remote credential backup pending explicit
approval. The local path has a real-age test covering recovery and forbidding
cloud calls. Operator-disk loss is not covered by this copy.

At 23:32 UTC both Prometheus replicas reported all 59 targets healthy, including
the three PostgreSQL instances. The read-only WireGuard observer ran on all six
hosts; queries at 23:37 found six healthy observers and thirty directed peer
records. That same verification retrieved the synthetic span using Tempo's
`mode=blocks` query, independently of its in-memory ingester. Loki and Tempo
object uploads were observed; Prometheus block uploads had not occurred yet.
These observations do not establish retention or failure recovery.

The first dashboard update stopped before Helm because CNPG serializes false
role privileges as absent. The ownership check now recognizes only those five
false privilege defaults and still rejects elevated roles, changed ownership,
connection limits or `ensure: absent`. The failed validation is retained, not
replayed. Five CPU diagnostic views are defined, with explicit missing-source
limitations; their live provisioning is a subsequent check. Updating dashboard
content preserves the original private-runtime risk-review expiry and findings.

Grafana plan `3e81c630d2a6663894d894270c0dcf7f919f2fdc1353a8c2fa7dd2d36c44d99a`
completed at 23:40:55 UTC with both replicas Ready. Verification found all five
provisioned views and executed 38 queries. Several storage panels were empty:
`overrideHonorLabels` had renamed workload labels to `exported_namespace` and
`exported_pod`. This is a correctness failure, not healthy storage evidence.
The correction restores authoritative KSM/kubelet workload identity during metric
relabeling, retains exporter labels under `scrape_*`, keeps global honor-label
protection and preserves pinned sample filters. Live verification follows rollout.

Monitoring plan `e57a4cf62bf95a799736f69217176a772e204f013b029e8cb3922df1c7cfbf61`
completed at 23:52:25 UTC with fail-closed admission retained. The repeated private
dashboard check at 23:53 executed all 38 queries: Pod/PVC panels now contain their
actual data. The only empty panel was firing alerts excluding Watchdog; missing
series are still not converted to zero.

Real-cluster CPU acceptance `a3f48c210e6b` deployed the private stub and completed
preparation Job UID `7964134a-b2ce-424e-bebd-c73b65a9a7b3`. All seven immutable
synthetic scenarios completed through management's actual runner Job protocol,
with registered artifacts downloaded and verified. WriterAssist and DocFlow had
valid delivery; session profiles exceeded the lag policy and both mixed paths
also missed admissions. These invalid results remain historical and unevaluated
for SLOs. They are not model performance or completed live acceptance.

The first WriterAssist run was `run-d499245b75f02100`, Job UID
`b3a0e9fc-9d1f-4719-9fde-6c5d8997dfab`, on platform-02 with the actual pinned
runner digest. Its completed artifact contained a timeline timestamp that management
had not retained. The finalization fix now requires a matching final observation,
result and origin, with a bounded deadline from Job completion rather than a
worker-local timer. Its management CI passed; a new live attempt must prove it.

A bounded read-only probe found clock-file reads around 1.7 ms, and twenty tiny
synthetic append/lock/flush cycles in a new diagnostic directory had a 16.87 ms
median and 26.60 ms maximum. Synchronous journal writes were in the HTTP hot path.
The new bounded background writer and cached immutable inputs passed 710 offline
tests (17 optional skips), Ruff/formatting, and all 17 native CPU AIPerf cases.
Slow-sink, overflow, I/O failure and missing-receipt checks fail closed. Publication
and fresh AWS acceptance of this change remain pending. No GPU has been rented.

The first journal image (`f857685`) was rolled on 4 November at 00:11:51 UTC.
Its ordinary management/backend/web checks passed, but Linux native/runner-Job
checks subsequently failed. The real browser proof imported the new bundle and
launched `run-45f07250760bebc0` (Job UID
`f2a2d2a9-3378-4a30-a43f-410d19614f28`); it failed and was not relaunched.
The private receipt preserves this failed gate. The stack remains an experiment
environment, and no live GPU or readiness claim follows from this rollout.

The timing strategy is a pure AIPerf component: it does not discover lifecycle
stop hooks. Linux process shutdown also bypasses ordinary `atexit`. The correction
drains the journal through a native TimingManager service stop hook, preserves
native service metadata and adds multiprocessing finalizers as a backstop.
Fork/spawn tests cover normal draining and deliberate abrupt loss; missing receipts
still fail. Management also waits a bounded interval for failed-Job partial
observations on shared storage and retains the original execution error. Root
checks passed 715 tests (17 optional skips); two focused native cases passed.
Linux CI and a separately identified AWS/browser attempt must pass before
acceptance. Earlier failed/invalid artifacts remain unchanged.

On 4 November, exact source `3b96a9d` passed Linux native workload, container,
management-Job, backend, frontend and management-integration CI. Management release
`188dee73757326d0a14a5dc84e47301adc8d44f507034c8499898925bd291e15`
completed at 00:39:32 UTC. Verification `36f98893a9f6` confirmed two replicas of
each application role on distinct CPU hosts, verified PostgreSQL TLS and shared
artifact access. The previous release's local plan/render/values were archived
before preparing the replacement.

Fresh synthetic cohort `e7a31b9c064f` prepared all seven immutable bundles with
Job UID `f2eb28cc-d4a5-47ab-8362-f70bd3b5d8da`. The browser imported WriterAssist,
launched its actual runner Job, inspected the result and downloaded its report
through the UI. The downloaded report SHA-256 was
`d240a34a0a75d214a19ee1608fbfb2855989680a7322840cc745d27fe88fc1cd`.
The remaining six runs used the same management import/launch/artifact protocol
sequentially. All seven completed with valid delivery, retained timing origins
and passing **synthetic smoke** verdicts:

| Scenario | Management run | Completed requests |
| --- | --- | ---: |
| WriterAssist | `run-7ec58204b9d5d875` | 24 |
| TutorChat | `run-e8ef1e668e6f459a` | 48 |
| DocFlow | `run-a00340ee2674eb1e` | 24 |
| ResearchAgent | `run-67d8f274128bd911` | 48 |
| CodeAgent | `run-d64256d95e8fcf1b` | 48 |
| Shared contention | `run-13caf8694e2c447c` | 120 |
| Platform mixed | `run-b56581281d3edcde` | 192 |

These 504 emulated requests verify CPU orchestration, native scheduling, bounded
journal shutdown, shared-storage finalization and report retrieval. They do not
calibrate model performance, prove failure recovery or establish GPU readiness.
Private receipts, downloaded artifacts and the credential-free browser screenshot
are retained under the operator-state `workload-acceptance/e7a31b9c064f` directory.
Earlier failed cohorts are preserved. No GPU had been rented at this checkpoint.

At 00:41, a Thanos query restricted to both healthy object-store gateways still
returned no historical metrics, matching zero sidecar block uploads. Its negative
store-selection control was also empty. This is recorded as pending cold-history
verification; successful live sidecar queries are not substituted for this gate.

At 01:09:58 UTC the repeated check passed: both Prometheus sidecars reported two
successful block uploads. Selecting only the two healthy object-store gateways
returned three deduplicated API-server targets at 23:45 UTC, while selecting a
nonexistent gateway returned no series. The retained `cold-history-proof.json`
therefore records `cold_history_verified: true`. This demonstrates cold history
retrieval for those uploaded blocks, not a completed retention/failure rehearsal.

On 4 November, the Lambda allocation boundary passed isolated PostgreSQL CI,
including concurrent dispatch and uncertain-response reconciliation. The bootstrap
callback passed database/API tests and generated-client checks. GPU runtime
preparation now uses its own pinned 2.3.7 artifact; the CPU runtime stays 2.4.2.
The first infrastructure check found an incomplete offline template context; the
correction now renders both runtime profiles and passed infrastructure CI.

Source `055d6f3` adds the typed isolated host-executor core and passed management,
contracts, infrastructure, image and native workload CI. Local checks passed
794 tests with 17 optional skips, Ruff/formatting and all 21 generated schemas.
Its transport/ownership tests do not establish live SSH/Ansible execution through
the new executor. Mutual-TLS service, dedicated image/storage/IAM, management
wiring and actual connectivity/GPU canaries remain next work. Real provisioning
is still disabled and no GPU has been rented.

Verification `a3f9501baca9` stopped at the private HTTPS readiness probe before
login; the old helper did not capture the underlying cause. The public health
endpoint subsequently returned 200, both unchanged API Pods/endpoints were ready,
all six nodes were ready and CNPG reported continuous archiving and a successful
02:00 backup. Follow-up `0f531385cc4e` passed the full management verification at
02:53:22 UTC. The initial failure is retained and is not treated as a measured
application outage or silently erased. The verifier now records failed status
and safe error class/status metadata without retaining credential-bearing errors
or adding authentication retries.

On 4 November at 03:03:42 UTC, the explicitly approved encrypted Grafana recovery
bundle was uploaded, read back by its exact S3 version and successfully decrypted
and compared. Its age key remains off S3. The new isolated host-executor service
then passed live storage/identity/SSH preflight and deployed two API replicas on
distinct platform hosts. Verification `e4fa9b7102cc` passed at 04:09:53 UTC,
covering real mutual-TLS positive/negative cases, restricted Kubernetes API access,
RBAC denials and exclusion of SSH keys from the management-namespace probe.
The original failed preflights (mount alias and OOM), the CIDR-policy connectivity
failure and their corrections remain preserved. Full offline verification passed
821 tests with 17 optional skips; Ruff/formatting and 21 schemas remain current.
The exact deployed image and seven-day residual-risk review are documented in
`lambda-lifecycle-integration.md`. Management's real lifecycle is not enabled and
no GPU has been rented. UI-driven allocation, GPU/software readiness, serving,
live inference and full recovery gates remain unfinished.

On 4 November, source `238b40a` installed GPU Operator 26.7.2 and standalone NFD
0.20.1 from checksum-verified charts and Linux AMD64 image digests. The older
bundled NFD is disabled. The three controller Pods became Ready on platform CPU
workers; verification at 05:57:52 UTC matched their runtime image IDs to the
reviewed digests. NFD workers require an executor-assigned protected candidate
label, and their desired count was zero. No GPU was allocated and no NVIDIA
driver was installed on a CPU host. The saved installation is
`fcd49d3649272058a2da50b93b45f3c32ce6eebfbd99cefcf65e71da5cd76783`.

Source `3922d93` then applied the narrowly scoped telemetry transition
`49b94b284d07d7312e05f54b3e0020fe62385266d9733405e4b6e2db9f3b464c`.
It adds the actual Prometheus discovery label to the DCGM ServiceMonitor and
permits only Prometheus-to-GPU-exporter TCP 9400 egress. The original install,
previous values/rendering, both Helm revisions and the update receipt remain
retained. Fresh installations include the corrected configuration directly.
See the [GPU Operator runbook](../runbooks/gpu-operator.md).

Post-update verification at 06:13:05 UTC confirmed six Ready CPU nodes, two Ready
replicas of each management component, three Ready PostgreSQL instances, three
Ready API-server/etcd Pods each, and both Prometheus replicas Ready. The first
verification stopped because it used a Pod component label against Deployment
metadata; exact Deployment-name checks corrected that verification assumption.
No application outage is inferred from that script failure.

The full offline suite passed 914 tests with 17 optional skips, Ruff/formatting,
21 generated schemas and management OpenAPI checks. Clean-cache chart fetching
also passed. Controller scans found no Critical findings; remaining High tuples
are explicitly reviewed against the private CPU-controller boundary with an
expiry. That review does not approve the GPU host runtime. Controller readiness
and configured scrape policies do not prove allocatable GPUs or ingested DCGM
measurements. Real host inspection, runtime security, UI-driven A10 admission,
serving and real inference remain required before those gates can pass.

At 08:20:18 UTC on 4 November, management verification `4aca565d3674`
passed after deploying source `bcbd1ec`: two Ready replicas per component on
separate CPU hosts, verified database TLS, authenticated HTTPS, all 36 historical
runs preserved, and shared-artifact reads/writes from each API/worker replica.
Release history is archived before replacing an active plan. Paid lifecycle
operations remain disabled.

At 08:33:44 UTC, prerequisite Job `lambda-prereq-d1b9a1b5bf0e45fb90e5`
completed; observation at 08:35:32 matched its native UID, PostgreSQL receipt,
executor public key, registered SSH key and dedicated nine-rule firewall.
The preceding image-startup failure is retained and was reconciled before a
new attempt; no provider mutation occurred in that failed attempt. The successful
setup allocates no GPU. Lambda's unchanged global SSH rule broadens effective
ingress; source-restricted bootstrap authentication and the later host firewall
are separate checks. No actual GPU, CUDA, serving or inference gate is inferred.

At 09:27:14 UTC, executor upgrade
`c32bfda766d779c8a8fdc95db4e0e724d27771f4931743741c278ed7edae3472`
completed with both exact new image digests Ready on separate CPU hosts. Its
native guard retained dispatch/peer-writer custody and verified strict SSH to
all six CPUs. Keys, TLS identities, peer revision 12 and storage were preserved.
The native scan retained zero Critical and the same 55 reviewed High tuples;
review expiry was not extended.

At 09:30:11 UTC, runtime staging
`c561cbadb37114931fea889118757c637eb05513d9edfeaddf8dde9045ab002e`
verified the complete assessment bytes on the executor volume. This permits
bounded A10 bootstrap/readiness, not public serving or a claim that unresolved
dependencies/kernel advisories are fixed.

Management release
`684cb6355002fae21e9dc677935a0e31024329140120e3ea18c19af8d49479df`
deployed at 09:32:41 UTC with the immutable bootstrap seed and provider-resource
binding. Verification `8aa19145275c` passed at 09:38:10: two replicas per role,
distinct protected hosts, verified PostgreSQL TLS, 36 retained historical runs,
authenticated HTTPS and artifact roundtrips through all API/worker replicas.
The earlier verifier failure counted completed provider setup Jobs under a
shared worker network label. The corrected check follows Deployment/ReplicaSet
UID ownership; the original failed evidence remains retained.

The public UI published `latency-workers@2`, preserving revision 1, and verified
the A10 SKU, region and 90-minute lease. Browser checks exposed operator-test
label/login/timing assumptions, corrected without changing authorization.
Virginia stock subsequently disappeared before any worker create was submitted.
A bounded UI stock watch preserves the approved region, type, price and
single-instance limits. Profile publication and an available-stock card do not
establish allocation, GPU readiness, serving or measured SLO results.

At 09:52 UTC Virginia stock returned and the public UI admitted
`writer-a10-20261104` as `op-ff9dd63e9ad344c4`. Execution failed before provider
dispatch because the binding resolved a mounted Secret to the API Pod's private
projection timestamp directory, absent in the worker Pod. The retained provider
intent was verified `pending`, with no `dispatched_at` and no instance IDs.
No GPU charge or readiness is inferred. The correction freezes the stable mount
path, retains credential fingerprints, and tests rotation/removal of the original
projection directory. Missing credentials now report a safe application error.
The original attempt/binding remain immutable; a fresh attempt requires the
corrected deployment and reconciliation of this unsubmitted intent.
