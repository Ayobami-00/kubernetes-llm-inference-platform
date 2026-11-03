# Ansible and management handoff

Terraform ends at AWS resources and access. It installs no Kubernetes packages,
WireGuard peers, certificates, CNI, Helm releases or GPU software. The next milestone
prepares these hosts with Ansible, establishes the direct host mesh before kubeadm,
and then bootstraps the three control-plane/stacked-etcd members.

## Allocations versus observations

`terraform output -json foundation_manifest` returns `foundation-allocation.v1`:
account/region/cluster, resource IDs, AZs, native/public addresses, separate disks,
credential references, planned WireGuard addresses, network ranges, API endpoint
and routed private API subnet CIDRs. This is infrastructure-owned metadata, not
evidence that a cluster exists. Keep operational exports outside Git.

```bash
make infra-export
```

The command independently verifies AWS resources and SSM host access, then writes
private `reports/foundation.json` (`foundation.v1`), `host-checks.json`,
`aws-verification.json`, `known_hosts` and `ansible-inventory.yaml` under the operator
state directory. Node/network observations use the existing shared models. The
foundation envelope stays in Stream B tooling; shared contracts/schemas are unchanged.

Host readiness becomes true only after successful SSM access and discovery of the
host's SSH public key. WireGuard, Kubernetes, Cilium, GPU runtime and model readiness
remain false until separately observed. Failed access produces truthful partial
observations and a failing command. Terraform allocations are never readiness flags.

Ansible hosts use the EC2 instance ID, user `ubuntu`, the local SSH credential alias
and `AWS-StartSSHSession` as ProxyCommand. Public SSH stays closed. Install AWS's
Session Manager plugin and use the foundation MFA profile on the operator machine.
Host public keys are obtained through authenticated SSM and pinned in the generated
known_hosts file with strict checking enabled. Private SSH key material remains
outside Terraform/Git; a local path in the private Ansible inventory is intentional.
An instance replacement requires refreshed observations and host-key pins.

## Full shared inventory after bootstrap

```bash
make infra-export EXPORT_ARGS='--ca-bundle /absolute/path/ca.crt --bootstrap-observations /absolute/path/nodes.json'
```

Both inputs are required together. The CA must be an existing public CA certificate,
not a key, must be unexpired and must verify a real TLS connection to the internal
API endpoint. Run this from an operator with private API routing (future WireGuard
or a controlled SSM tunnel with appropriate DNS/SNI handling). `nodes.json` contains
real shared `NodeObservation` objects from bootstrap checks, including Kubernetes
node names/UIDs and observed readiness. Provider IDs/addresses must match the current
foundation. The exporter then emits `infrastructure-inventory.json`, validating
against `InfrastructureInventory`. Missing CA/bootstrap evidence fails clearly;
the code does not fabricate a CA reference or mark fixtures as live infrastructure.

## Later Stream C invocation contract

C owns Lambda allocation/termination and durable job retries. B will provide
idempotent host-preparation/join/removal entrypoints in the next Ansible slice.
The expected inputs are:

| Input | Owner/source |
| --- | --- |
| Cluster ID and validated full inventory | B bootstrap observations |
| Lambda instance ID, public/native address and region | C allocation result |
| Worker-group configuration and desired node identity | Shared worker-group contract/C job |
| Reserved WireGuard address `.64`–`.254`, peer public key and current peers | B network allocator/workflow |
| SSH/host-access credential references | Secret provider; references only in job/inventory |
| Short-lived join token, CA hash and API endpoint | B bootstrap/join workflow |
| Removal node identity, drain timeout and job idempotency key | C durable removal job |

Repeat network overlap checks against each Lambda host's actual local networks.
Establish direct peers, MTU, forwarding, API routes/return paths and peer failover
before join. GPU-to-GPU traffic uses direct peers. A GPU VM joining Kubernetes and
being GPU-runtime-ready are distinct from model-serving readiness.

Removal will drain workloads, remove the Kubernetes node and stale WireGuard peers,
then report completion so C can terminate the VM. Define forced/failure handling
explicitly in that slice; do not describe these future commands as implemented.
C needs no AWS foundation/DNS write identity. Inference gateway origin changes use
the separate foundation-operated DNS workflow. No custom operator/CRD/controller
is introduced.
