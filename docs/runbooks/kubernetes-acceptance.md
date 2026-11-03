# Live CPU cluster acceptance — 3 November 2026

The authorized rollout in account `127811062476`, London (`eu-west-2`), completed.
The clean acceptance repeat ran from 08:11 to 08:22 UTC, followed by standalone,
backup-service and cleanup checks. All three original CPU instances remain running.
No foundation host, disk or address was replaced.

## Observed results

| Check | Result |
| --- | --- |
| Kubernetes | Three Ready control-plane nodes, Kubernetes 1.36.5, containerd 2.4.2 |
| etcd | Three healthy stacked members, image `registry.k8s.io/etcd:3.6.8-0` |
| API | Three healthy private NLB targets; real hostname and CA verified through SSM and the external peer |
| WireGuard | Direct CPU mesh, fresh handshakes and 1420-byte DF packet delivery |
| Cilium | 1.20.4, VXLAN over WireGuard; no additional Cilium encryption |
| Pod MTU | Interface 1420, default-route MTU 1370; cross-node 1370-byte packets succeed and oversized DF packets are refused on all CPU nodes |
| Pod networking | Three smoke replicas, one per CPU; cross-node Pod, Service and DNS connections succeed |
| Visibility | Hubble flow records observed; two available replicas each of CoreDNS, Hubble relay and metrics-server; node metrics collected |
| Peer restrictions | External peer reaches TCP 6443; direct CPU etcd and kubelet connections are blocked |
| Secret protection | Disposable Secret observed as AES-CBC ciphertext in etcd; encryption configuration hashes match on all CPUs |
| Reruns | Host preparation preserved disks/configuration; bootstrap preserved CA and etcd member identities |

The external VM used `10.41.0.0/24` in a separate, non-peered VPC, without a native
private route to the cluster VPC. API access used WireGuard and scoped CPU-gateway
routing/SNAT. The final repeat stopped active gateway cp-01. **All recovery conditions
completed in 40.1 seconds**: authenticated writes succeeded through cp-02 and the
stopped NLB target was unhealthy. Six writes failed transiently during convergence.
Ten subsequent writes succeeded, surviving etcd quorum was verified, and cp-01
recovered. This measures new requests; existing TCP connections can reset.

## Backups and worker lifecycle

The retained, private, versioned S3 bucket is encrypted and blocks public access.
Daily backups are staggered at 02:00, 02:15 and 02:30 UTC, with jitter. Each CPU
uploads an age-encrypted bundle only to its own IAM-session prefix. Independent
recovery-key custody was verified and confirmed.

All three installed systemd services ran with `Result=success` and `ExecMainStatus=0`.
Their exact S3 versions downloaded, decrypted and passed snapshot identity/checksum
checks. A snapshot also restored into isolated etcd on the test VM, where Kubernetes
registry keys were read. Restore files/containers were removed. Full three-host
disaster recovery was not rehearsed. See [recovery and retention](cluster-recovery.md).

The clean repeat exercised worker preparation, mesh, join and removal without
corrective intervention. Renumbering `.2` to `.64` left exactly one WireGuard address
and working gateway probes. The worker received a validated CA-signed kubelet serving
certificate and became Ready with healthy Cilium. Removal drained/stopped it, revoked
peers and deleted the matching node by its observed UID. GPU readiness stayed false.

## Cleanup and validation

The initial rollout exposed and corrected kubelet package-marker classification,
SSM setup, Cilium default-deny behavior, MTU verification, worker address/certificate
reconciliation and stale EIP association ownership. IAM was not widened to bypass
the EIP error. A fresh VM verified the corrected paths. Both sequential test VMs
and their AWS resources are removed; combined usage stayed within the approved
four probe-hours.

The probe Terraform state is empty. Bootstrap, foundation, backups and empty-origin
inference DNS have zero-change follow-up plans. No inference A records or health
checks were introduced. Smoke resources, probe service-account permissions, join
tokens and temporary listeners are removed. Standalone verification creates and
cleans its own smoke fixture.
The final credential audit also revoked kubeadm's original implicit initialization
token and confirmed zero bootstrap-token Secrets remain. New initializations pin
that token to 15 minutes and revoke it explicitly after success.

Local validation passed: **279 Python tests**, **13 Terraform provider-mock tests**,
formatting/backend-free initialization/validation/TFLint for nine roots/modules,
production Ansible lint, inventory/templates, and three Helm releases rendering
36 resources. Ruff passed; all twelve schemas remain current. Eleven IAM documents
and forty-six simulations passed the rollout review; no subsequent IAM-policy
changes were needed. No hosted CI run is claimed.

Private evidence stays under `~/.local/state/kubernetes-llm-inference-platform/`:
`cluster/acceptance.json`, `cluster/verification.json`,
`cluster/systemd-backup-verification.json`, `cluster/backup-verification.json`,
`cluster/packet-size-verification.json`, `cluster/fixture-cleanup-verification.json`,
`cluster/final-host-and-cleanup.json`, `cluster/final-drift.json` and
`cluster/images-observed.json`. Original receipts/corrective-run evidence remain
in `cluster/history/`. Keys, kubeconfigs, decrypted archives and operational reports
are excluded from Git/Terraform state.

## Handoff

`reports/infrastructure-inventory.json` validates against the unchanged shared
`InfrastructureInventory`, with a real CA reference, observed node UIDs and verified
host/WireGuard/Kubernetes/Cilium readiness. GPU readiness is false.

C's next interface is the [versioned worker request/result and peer registry](worker-host-operations.md),
including authenticated executor and short-lived credential issuance integration.
C retains Lambda allocation/termination and durable jobs; the backend receives no
AWS write identity or operator admin kubeconfig. Actual Lambda/GPU validation,
inference/model serving, application deployment and full observability remain later
milestones.
