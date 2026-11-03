# End-to-end platform delivery

The active milestone connects the existing workload, infrastructure and management
implementations. Completion requires live evidence and a retained-state rebuild; local
rendering or a successful stub run does not satisfy a live gate.

## Acceptance ledger

| Gate | Required evidence | Current status |
| --- | --- | --- |
| Persistent platform | Separate CPU workers, real storage, management spreading, PostgreSQL HA and restore | CPU workers, EFS/EBS and replicated PostgreSQL with external backup/restore live; management migration/failover pending |
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
