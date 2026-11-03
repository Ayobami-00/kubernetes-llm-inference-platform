# Persistent platform worker allocation

The `platform` Terraform root adds three CPU workers to the existing project VPC,
one per configured London AZ. It owns no control-plane instance, etcd volume or API
load balancer. Its foundation reference is read-only and an account/project/cluster
mismatch fails before provisioning.

`infra/terraform/platform.json` defines reviewed slots: `platform-01` through
`platform-03`, `m7i.xlarge` (four vCPUs / 16 GiB each), encrypted 80 GiB root scratch,
and WireGuard addresses `.64` through `.66`. These addresses are reserved; the GPU
allocator must exclude them, including before the hosts become Ready. Persistent
workers are not Lambda worker groups and have no GPU lease.

This root was applied on 3 November 2026 after a reviewed create-only plan and live
quota checks. All three hosts subsequently joined and passed the trusted host,
network, certificate and resource-reservation checks. Storage, scoped workload identity,
application HA and recovery gates remain pending. An allocation output does not
claim joined or healthy Kubernetes workers.

## Cost and authority

The user authorized sensible provisioning without a numerical monthly ceiling.
The initial envelope is three persistent CPU workers and, later, one A10 at a time.
Do not add speculative GPU capacity or automatically change the selected SKU/region.

The AWS Price List Query API on 3 November 2026 returned SKU `RURAAFB8XBQ2MN9M` at
USD 0.2331/hour for Linux/shared/on-demand `m7i.xlarge` in London. Three continuously
running workers cost approximately USD 510.49 for 730 hours of compute. This excludes
volumes, public IPv4, RWX/object storage, traffic, tax and GPUs; refresh the actual
resource estimate before applying. The full quote is retained in ignored operational
artifacts, not copied into a deployment pin or customer SLO.

The regional quota check found five Elastic IPs and 32 Standard on-demand vCPUs.
The proposed total needs six IPs and 18 vCPUs. An automatic-only IP quota increase
to eight was approved and effective before application; no support case was
authorized or created. The operator independently rechecks
current regional usage and additional plan requirements immediately before apply,
so an approved plan alone cannot bypass missing quota.

The private live preview initially contained 23 creates (including the local
foundation-identity guard), no updates/replacements/deletions, and no control-plane
resource mutations. Preview plans cannot be applied. Generate a fresh live saved
plan after the IAM rollout and after any source changes.

## Ownership and deployment

The foundation deployer receives only the additional `klip-platform-worker` bounded
node-role/profile scope and the `platform-workers/terraform.tfstate` state/lock keys.
The worker role attaches SSM core only. Application/storage controllers must use
separate scoped identities, never this host role or the foundation deployer identity.
The management backend receives neither identity.

Initial ingress permits WireGuard and path-MTU ICMP. Public SSH, Kubernetes API and
application ports are closed. Later storage/gateway rules require explicit scoped
definitions. Use SSM for administration and pinned SSH host keys for Ansible.

```sh
make infra-platform-preview
# Review the generated IAM diff and bootstrap saved plan before its authorized rollout.
make infra-bootstrap-plan
make infra-bootstrap-apply PLAN_SHA=<reviewed-bootstrap-digest>
make infra-platform-plan
make infra-platform-apply PLAN_SHA=<reviewed-live-platform-digest>
make cluster-platform-join
```

Review saved plans against the exact project account, state and source digest. The
normal operator rejects deletions/replacements and never applies a preview plan.
The live root uses native S3 locking in its own state key.

`cluster-platform-join` holds the operator lock and reuses B's versioned host
prepare/mesh/join interface. It verifies live EC2 identity, addresses, security,
volumes and AZ before establishing pinned SSM-backed SSH access. Changed host keys
fail closed. Join credentials live only in private short-lived files and are
revoked after use, including failed joins.

New nodes start with `klip.dev/bootstrap=true:NoSchedule`. Cilium tolerates this
taint so networking can initialize; application placement waits for actual
Kubernetes/Cilium readiness, kubelet serving certificates, API gateway routes and
resource reservations. The final label/taint patch includes a Kubernetes UID
precondition and preserves unrelated taints. These hosts have no etcd volume and
join as workers. The per-node receipt is private `cluster/platform-workers.json`;
requests, operation status and logs remain under private `cluster/operations/`.

If the CPU-era Cilium release lacks the bootstrap toleration, reconcile the
versioned Helm values before retrying the worker command:

```sh
uv run --locked --group infrastructure python scripts/infrastructure/cluster.py sync-release --release cilium
make cluster-platform-join
```

Static release reconciliation holds the same operator lock. Cilium's rolling
strategy permits at most one unavailable agent. The operator derives Cilium's
`k8sServiceHost`/port from the validated foundation's private API load balancer;
agent initialization must not depend on the ClusterIP Service path it helps serve.
This runtime Helm input is regenerated after rebuild, not hardcoded to an old NLB.
Explicit retries recheck identities
and reconcile existing membership; they never run kubeadm reset or initialize etcd.
Foundation verification continues to report only its three control-plane hosts
when known worker identities are also present; worker readiness is a separate proof.
Operator Helm/kubectl access uses a dedicated SSH local forward over the same
SSM-backed, host-key-pinned transport. It binds loopback only, does not share a
persistent SSH master, verifies the Kubernetes CA/SNI before use and closes the
owned forward on exit. The Kubernetes API remains private.

After joining, run `make cluster-verify-platform`. It creates an isolated test
namespace and one small, digest-pinned test Pod per platform worker. It verifies
Service/DNS access and Pod connections across workers, the effective Pod route MTU,
and metadata isolation. Transport/authentication failures cannot count as a successful
metadata-denial check. It checks namespace ownership before cleanup and waits for
deletion. The latest report is private `cluster/platform-network-verification.json`;
each verification also retains its own report under `cluster/operations/`.

## Teardown boundary

Root disks contain runtime scratch. PostgreSQL/telemetry data requires separately
retained storage and verified external backups. Termination protection is enabled.
Do not bypass it with manual termination or reuse the control-plane destroy command.
The integrated platform drain/retained-state teardown orchestrator is a later gate;
implement and verify that procedure before destructive acceptance.

The final workflow must drain workloads, verify backup/restore material, remove
Kubernetes/mesh identities by UID, disable protection through a reviewed plan and
then destroy this allocation. Retained data and bootstrap state are separate owners.


## Protected placement

`make cluster-platform-protect` rechecks Node authorization and NodeRestriction on
all three API servers and applies `klip.dev.node-restriction.kubernetes.io/platform=true`
only to the recorded CPU instance/node UIDs. Each actual kubelet must fail a server-side
dry-run self-label attempt. Other errors fail the verification. Normal platform joins
also add this administrator-owned label after readiness checks. Management, runner and
PostgreSQL placements use it; the older `klip.dev/node-role` label is descriptive.
This prevents an ephemeral kubelet from self-selecting platform credentials/workloads.
The command does not join, reboot or replace hosts. Results are retained in
`cluster/platform-placement.json` in operator state.
