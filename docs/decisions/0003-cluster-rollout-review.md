# CPU cluster rollout review — 3 November 2026

Status: implementation prepared; persistent AWS/host rollout is not yet authorized.
The prior foundation acceptance remains valid historical evidence. No Kubernetes,
WireGuard, Cilium or backup readiness is claimed before this rollout's live checks.

## Reviewed changes

| Terraform root | Planned changes | Ownership |
| --- | --- | --- |
| bootstrap | 3 creates, 3 in-place updates | Two scoped policies, one deployment-role attachment; extend bounded node/probe access and the SSM tunnel document |
| foundation | 1 create | Attach write-only backup policy to the existing CPU role |
| cluster-backups | 7 creates | Retained, private, encrypted, versioned S3 bucket with lifecycle rules |
| network-probe | 20 creates | Temporary separate VPC, t3.medium, 20-GiB root, explicit ENI/EIP and SSM-only identity |

There are no proposed CPU instance, EIP or disk replacements/deletions. Existing
instance/NLB protections stay enabled. The DNS root and inference origins do not
change. New-root previews cannot be applied; after the IAM update, recreate their
plans against the existing S3 backend and exact reviewed state keys.

All three hosts have been reached through authenticated SSH-over-SSM with strict
host-key pins. Read-only inspection found synchronized clocks and three raw,
unformatted 20-GiB dedicated disks. Host changes will install pinned packages and
runtimes, format only those verified empty disks, mount by UUID, configure scoped
firewall/WireGuard services and bootstrap the three control-plane members.

kubeadm uses the real NLB hostname, WireGuard node/etcd addresses, API SANs covering
native/WireGuard endpoints, Secret encryption and explicit static-Pod resource
requests. Taints stay in place. Helmfile installs Cilium/Hubble, metrics-server and a Cilium workload metadata-deny policy;
CRDs install explicitly. No GPU software, management application or model release
is included. Terraform never installs persistent software on the CPU hosts.

The CPU IAM role remains shared. Backup writes are restricted to
`clusters/klip/${aws:userid}/*`; for EC2 role credentials that identity includes the
role unique ID and instance ID. The operator reads the role ID and exports the
matching prefix into private host configuration. Each node cannot write another
instance's prefix or read/delete backups. The ARN-variable alternative was rejected
by permission simulations before any apply. No deployment-role self-modification
or application AWS credentials are introduced. See [AWS policy variables](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_variables.html).

The recovery-key copy has been verified without exposing its contents; the owner
confirmed independent retention. Backup readiness additionally requires actual
versioned uploads, freshness, enabled timers and an isolated restore check.

## Added cost

The existing approximately **USD302.71/month** foundation baseline is unchanged.
London AWS Price List results were read on 3 November 2026:

| Item | Estimate |
| --- | ---: |
| Temporary t3.medium, four hours at USD0.0472/hour | USD0.1888 |
| Temporary 20-GiB gp3 root, four hours | approximately USD0.0102 |
| Temporary public IPv4, four hours | USD0.0200 |
| Total temporary test baseline | approximately **USD0.22** |
| Backup S3 Standard storage | **USD0.024 per GB-month**; 10GB of billable storage is USD0.24/month |

S3 requests and variable network traffic are additional. Storage depends on real
snapshot sizes and all retained versions, rather than a fixed monthly reservation.
Lifecycle expires current objects after 30 days and noncurrent versions after 30 days;
physical/versioned bytes can therefore outlive the current-object window. There is
no KMS-key fee. IAM and host services add no separate hourly resource charge.

The probe's absolute four-hour shutdown deadline survives reboot and terminates
compute. Cleanup must also remove its retained EIP/ENI/network/IAM resources; the
expiry timer alone does not remove them. Cross-AZ/WireGuard traffic remains variable.
See [AWS EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/),
[S3 pricing](https://aws.amazon.com/s3/pricing/) and
[VPC public IPv4 pricing](https://aws.amazon.com/vpc/pricing/).

## Validation boundary and rollout

Local checks cover all nine Terraform roots/modules, provider-mocked tests, Ansible
syntax/production lint and role templates, example inventory parsing, all static
Helm releases, Python failure/encryption tests, Ruff and all twelve generated schemas.
Access Analyzer validates all eleven IAM documents and forty-six permission
simulations cover both permitted and denied operations. Private reports retain the
actual outputs. CI receives no deployment credentials; no hosted CI run is claimed.

Live acceptance will verify real Kubernetes/etcd health, NLB targets, direct peers,
Pod/Service/DNS communication, actual Pod MTU/VXLAN paths and Hubble flows. It will
stop the active CPU gateway (initially cp-01), require new authenticated API writes from the separate network to recover
within 90 seconds, then restart it. Transient failures and resets are recorded. The
probe additionally tests a generic worker join/removal and isolated etcd restore.
This does not establish Lambda/GPU compatibility or full three-host disaster recovery.

Only after explicit rollout authorization: apply bootstrap IAM, retained backups
and the CPU policy attachment; prepare/mesh/bootstrap/deploy; create the probe and
run acceptance. Remove test credentials/resources, preserve all three running CPU
hosts, export genuine inventory, and verify clean foundation/bootstrap/DNS/backup
plans. Probe state must be empty after cleanup. Commit results on feat/infrastructure;
never merge or push to main automatically.

C's next interface is the versioned worker request/result and peer registry. C owns
allocation/termination, durable jobs and deployment of its authenticated executor.
B's initial operator commands do not grant the management application B's admin
kubeconfig or AWS write identity. Lambda SSH preparation requires declared executor
/32 sources; AWS CPU/probe SSH remains closed publicly.

## Recorded local review

The final local suite passed: **265 Python tests**, **13 Terraform provider-mock
tests**, formatting/initialization/validation/TFLint for all nine roots/modules,
production Ansible lint, role-template and inventory checks, and lint/rendering for
three Helm releases (36 resources). Ruff passed and all twelve schemas are current.
All eleven IAM documents passed Access Analyzer; all forty-six simulations passed.
SSH-over-SSM was verified on each CPU host. Kubernetes live acceptance is pending.

Reviewed implementation SHA-256: `0b44ade763c062fa9f8c93ab9f196cfb3cf16ce3fe9eeb6bf521c3f10e856c61`.

| Plan | Applicable before IAM rollout? | SHA-256 |
| --- | --- | --- |
| bootstrap | Saved real plan | `6dbec13f7cd38e77f65d007e03b39d3c0c9885ef58e2e801a01103732ddd2467` |
| foundation | Saved real plan | `c8097da1fa547b49d183e1259b4abf504d58f968e99797156904ab087bd245d5` |
| backups-preview | No; preview only | `8ea0bb0e1f7d47250ab43b40b8e1bd61b674c726b08864af15bc33ebe7fcde41` |
| probe-preview | No; preview only | `0784e185207d33d6ef9d6b5f1a19a08c28d938d24040d38554447c991f5d17bc` |

## Rollout outcome

Live rollout for commit `ae9f6c9` was subsequently authorized. The
[acceptance report](../runbooks/kubernetes-acceptance.md) records the applied resources,
corrections, 40.1-second measured gateway recovery, verified backups/restore,
automated worker lifecycle repeat and cleanup. The table above remains the original
review record; those plans must not be reused after source changes. Final zero-change
plans and their source hashes remain in the private operator evidence.
