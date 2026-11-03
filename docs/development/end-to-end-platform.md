# End-to-end platform delivery

The active milestone connects the existing workload, infrastructure and management
implementations. Completion requires live evidence and a retained-state rebuild; local
rendering or a successful stub run does not satisfy a live gate.

## Acceptance ledger

| Gate | Required evidence | Current status |
| --- | --- | --- |
| Persistent platform | Separate CPU workers, real storage, management spreading, PostgreSQL HA and restore | CPU workers, EFS/EBS, replicated PostgreSQL and migrated management history live; management deployment/failover pending |
| Lambda lifecycle | UI allocation, trusted bootstrap, joined network-ready GPU worker, safe cleanup | Pending |
| GPU runtime | Operator-owned stack, allocatable GPU, CUDA validation and health telemetry | Pending |
| Serving | Pinned Qwen3-4B through authenticated HTTPS gateway and llm-d | Pending |
| WriterAssist | Real JFLEG canonical run, valid delivery, GLEU and reconciled SLO verdict | Pending |
| Observability | Provisioned metrics/logs/traces, twelve views, alerts and diagnostic links | Pending |
| Resilience | Stable run identity through management/database faults and sustained runs | Pending |
| Rebuild | Actual retained-state teardown, restore, fresh UI-provisioned A10 and repeated run | Pending |
| Expansion | Sequential A100 profiles, separate mixed capacity, all scenarios and failure campaign | Pending |

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
