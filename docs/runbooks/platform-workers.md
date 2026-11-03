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

This root is implemented and passes provider-mock checks. Host rollout, storage,
identity, application HA and recovery gates remain pending. An allocation output
does not claim joined or healthy Kubernetes workers.

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
to eight was requested; no support case was authorized or created. Wait for the
effective quota to increase before applying. The operator independently rechecks
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
```

Review saved plans against the exact project account, state and source digest. The
normal operator rejects deletions/replacements and never applies a preview plan.
The live root uses native S3 locking in its own state key.

After allocation, use B's versioned worker prepare/mesh/join interface. Verify EC2
identity/AZ, trusted host access, WireGuard, kubelet certificates, Cilium, labels and
resource reservations before enabling platform workloads. Apply topology labels from
verified observations. These hosts have no etcd volume and join as workers.

## Teardown boundary

Root disks contain runtime scratch. PostgreSQL/telemetry data requires separately
retained storage and verified external backups. Termination protection is enabled.
Do not bypass it with manual termination or reuse the control-plane destroy command.
The integrated platform drain/retained-state teardown orchestrator is a later gate;
implement and verify that procedure before destructive acceptance.

The final workflow must drain workloads, verify backup/restore material, remove
Kubernetes/mesh identities by UID, disable protection through a reviewed plan and
then destroy this allocation. Retained data and bootstrap state are separate owners.
