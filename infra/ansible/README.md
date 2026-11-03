# Host automation

Run these commands from the infrastructure worktree. `make cluster-tools` installs
locked operator tools and verifies Linux host artifacts. All inventory and secrets
are under `KLIP_STATE_HOME` (default `~/.local/state/kubernetes-llm-inference-platform`).
No playbook runs from Terraform. No playbook resets a cluster or wipes existing data.

`prepare.yaml` installs OS/runtime prerequisites and guards the dedicated etcd mount.
It does not initialize Kubernetes. `mesh.yaml` reconciles the public peer registry
under a host lock and installs scoped firewall chains. `backup.yaml` installs daily
encrypted snapshots after recovery-key custody is verified. The Python operator
coordinates kubeadm sequentially, keeping tokens and private PKI out of Ansible logs.

Use `make cluster-check` for credential-free syntax, production Ansible lint and Helm
checks. Read [the rollout runbook](../../docs/runbooks/cluster-bootstrap.md) before
running any mutable command. Inventory is generated; do not commit operational exports.
