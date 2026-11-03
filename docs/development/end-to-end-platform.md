# End-to-end platform delivery

The active milestone connects the existing workload, infrastructure and management
implementations. Completion requires live evidence and a retained-state rebuild; local
rendering or a successful stub run does not satisfy a live gate.

## Acceptance ledger

| Gate | Required evidence | Current status |
| --- | --- | --- |
| Persistent platform | Separate CPU workers, real storage, management spreading, PostgreSQL HA and restore | CPU workers live and joined; storage, management and PostgreSQL rollout pending |
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
