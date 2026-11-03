# AWS foundation acceptance — 3 November 2026

The owner authorized provisioning and live acceptance for account `127811062476`
in London at the approximately USD303/month baseline. The AWS foundation is live;
all three CPU hosts remain running. This report records AWS infrastructure and
transport acceptance, before Kubernetes bootstrap.

## Provisioned and observed

| Host | AZ | Native address | Planned WireGuard address |
| --- | --- | --- | --- |
| cp-01 | eu-west-2a | 10.40.0.10 | 10.200.0.11 |
| cp-02 | eu-west-2b | 10.40.1.10 | 10.200.0.12 |
| cp-03 | eu-west-2c | 10.40.2.10 | 10.200.0.13 |

- Three running on-demand m7i.large hosts use the pinned Canonical Ubuntu24.04 AMI,
  stable primary ENIs and retained Elastic IP allocations.
- All six gp3 disks are encrypted, with 40-GiB roots and separate 20-GiB etcd disks.
  Root deletion and separate-volume retention settings match the intended lifecycle.
- SSM access succeeds on every host. The actual node role has only SSM core and the
  intended permissions boundary. IMDSv2, hop limit one and termination protection
  were checked against AWS.
- Security rules match the reviewed set exactly. Public TCP22/6443 checks fail as
  intended. CPU subnets route through the IGW; private API subnets have only local
  routing. ENI source/destination checking is disabled for later mesh forwarding.
- The internal IPv4 TCP6443 NLB has the three static private frontends, cross-zone
  balancing, deletion protection, private-IP targets and disabled client-IP
  preservation. Its configured health-check interval/thresholds were verified.
- The state bucket's versioning, AES256 encryption, owner enforcement, public-access
  blocking and TLS-only policy were verified directly. All three roots use separate
  S3 state keys and native locks. Existing access objects were adopted successfully.
- The separate DNS identity applied empty origins. No inference A records or
  gateway health checks were created; the existing registrar/zone remain retained.

## Live failure test

The prebootstrap test ran from `05:19:29` to `05:22:07` UTC. It guarded all hosts
against an existing cluster/API listener, installed only expiring temporary HTTP
listeners on6443, and confirmed all three targets healthy.

Each host completed 20 successful requests through the shared NLB endpoint: 60
initial successes. After stopping cp-03, the convergence sample recorded 27
successes and **three refused connections**. After health-check convergence,
20/20 new requests succeeded through cp-01/cp-02, with no cp-03 responses.

The finalizer restarted cp-03, verified SSM and target recovery, stopped every
temporary listener and removed its runtime files. Cleanup reported no errors.
A subsequent observation confirmed all three NLB targets became unhealthy after
listener removal, as expected until actual Kubernetes API servers are installed.
These results do not establish uninterrupted connections during convergence.

## Local validation and final drift checks

The final credential-free suite passed: 239 Python tests (including all original
137 contract tests), 11 Terraform mock tests, formatting/init/validation and TFLint
for all three roots/four modules, Ruff lint/format and all twelve schema checks.
Shared models and schemas remain unchanged. All nine final IAM documents passed
Access Analyzer without findings, and all 35 allow/deny simulations passed.

Post-provisioning plans were clean for bootstrap, foundation and inference DNS:
zero resource changes. Private receipts retain the exact plan/source hashes.

Live testing exposed and resolved these configuration issues within the approved
scope: a metadata-only first S3 migration discrepancy, separate EC2 parent/new-rule
authorization requirements, a provider Elastic IP attribute read and an instance
default competing with ENI-owned forwarding. Both state copies/S3 versions were
retained during metadata recovery; the EIPs were verified before clearing
creation-error taints. No hosts, addresses or disks were destroyed/replaced to
bypass a failure. See the state/provisioning runbooks for recovery procedures.

## Handoff and limits

Private operational artifacts are under
`~/.local/state/kubernetes-llm-inference-platform/reports/`:

- `foundation.json`: validated `foundation.v1` allocations and live node/network
  observations; `host-checks.json` records SSM evidence.
- `ansible-inventory.yaml` and `known_hosts`: SSH-over-SSM inventory with authenticated
  host-key pins. Private key material remains outside Terraform and Git.
- `aws-verification.json`, `state-bucket-verification.json`, `iam-validation.json`,
  `smoke.json` and `post-smoke-cleanup.json`: actual acceptance evidence.

Host readiness is true. WireGuard, Kubernetes, Cilium and GPU runtime readiness
remain false. No full `InfrastructureInventory` was fabricated: its export still
requires a real CA and bootstrap observations. The next milestone is Ansible host
preparation, the direct WireGuard mesh and kubeadm bootstrap, followed by Cilium.
C's future join/removal inputs and credential references are documented in the
infrastructure handoff; C retains Lambda VM lifecycle ownership and receives no
AWS foundation/DNS write credentials.

Registrar contact verification remains pending. Complete the registrar email
verification separately before relying on future public inference origins.
