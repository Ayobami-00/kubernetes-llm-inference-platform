# Infrastructure

Terraform allocates cloud resources; Ansible configures hosts and the self-managed
cluster. Kubernetes releases and model workloads have separate deployment owners.

| Location | Responsibility |
| --- | --- |
| `terraform/state-bootstrap/` | Remote state and access-bootstrap inputs |
| `terraform/environments/` | Independently reviewed foundation, workers, networking, storage, identity, registry, frontend and observability roots |
| `terraform/modules/` | Reusable compute, API endpoint and DNS modules |
| `terraform/*.json` | Project-specific account, region, domain and identity settings |
| `ansible/playbooks/` | Host preparation, kubeadm and worker-operation entrypoints |
| `ansible/roles/` | Container runtime, host network, Kubernetes, security and telemetry configuration |
| `ansible/inventory/` | Inventory format and handoff documentation |

The cluster uses three persistent AWS control-plane nodes with stacked etcd,
separate CPU workers, host WireGuard and Cilium Pod networking. Lambda GPU VMs join
as workers. Terraform does not rent Lambda capacity; provider/host operations use
separate guarded interfaces. Retained external GPU instances have stricter ownership.

Review the existing environment inputs before using this code for another account.
State, plans, credentials and private inventory remain outside Git. Teardown keeps
retained data, signing identities and recovery material separate from compute.

Start with [Terraform](terraform/README.md), [Ansible](ansible/README.md),
[foundation provisioning](../docs/runbooks/aws-foundation.md),
[cluster bootstrap](../docs/runbooks/cluster-bootstrap.md) and
[teardown/recovery](../docs/runbooks/foundation-teardown.md).
