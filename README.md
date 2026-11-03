# kubernetes-llm-inference-platform

A Kubernetes-native LLM inference platform built with llm-d and vLLM, designed to support multi-tenant serverless, provisioned-throughput, and dedicated GPU serving. It explores SLO-aware routing, tenant isolation, GPU capacity planning, and cost-efficient serving through realistic customer workload simulation, performance experiments, fault injection, and end-to-end observability.

## Architecture

The platform combines independently deployable GPU serving profiles with a persistent CPU environment for control, observability, and experiment results.

```mermaid
flowchart TB
    C["Customer simulator<br/>Workloads, contracts, sessions, faults"]
    G["Gateway<br/>Authentication, admission, limits, usage"]
    R["Authorized pool selection<br/>Tenant + model + service tier"]

    C --> G --> R

    R --> L["Dedicated large pool<br/>8 GPUs · llm-d + vLLM"]
    R --> A["Shared model A<br/>2 replicas · 1 GPU each"]
    R --> B["Shared model B<br/>2 replicas · 1 GPU each"]
    R --> S["Dedicated latency pool<br/>1 GPU · llm-d + vLLM"]

    P["Persistent CPU environment<br/>Kubernetes control plane, management services,<br/>observability and experiment results"]
    P -. configuration and lifecycle .-> G
    P -. configuration and lifecycle .-> R
    P -.-> L
    P -.-> A
    P -.-> B
    P -.-> S
```

## Shared contracts

The first implemented layer is an offline Python contract package. It defines customer requirements,
traffic scenarios, frozen run inputs and results, Lambda worker groups, serving profiles, operation
progress, and infrastructure inventory. It includes all five customer examples:

| Customer | Workload | Dataset basis | Serving candidate |
| --- | --- | --- | --- |
| WriterAssist | Sentence correction | JFLEG | Dedicated Qwen3-4B |
| CodeAgent | Recorded coding-call replay | Exgentic v2 / SWE-bench | Dedicated Qwen3-235B-A22B |
| TutorChat | Multi-turn tutoring | MathDial | Provisioned share of Qwen3-8B |
| DocFlow | Document-text extraction | CUAD | Best-effort Qwen3-14B |
| ResearchAgent | Prepared research QA steps | HotpotQA | Provisioned share of the same Qwen3-14B |

All traffic, SLO and hardware settings are experimental defaults. The source manifests are not
prepared datasets, and the runtime fixtures contain synthetic measurements. No cloud resources,
inference services, dataset preparation, or benchmark execution are implemented by this layer.

### Install and verify

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run these commands from the
repository root. uv uses the Python 3.12 version selected by `.python-version`.

```bash
uv sync --locked --all-packages
uv run --locked --package platform-contracts pytest
uv run --locked --package platform-contracts ruff check .
uv run --locked --package platform-contracts ruff format --check .
bash scripts/check-generated.sh
```

The package can load and resolve the examples without network access after installation:

```bash
uv run --locked --package platform-contracts python - <<'PY'
from pathlib import Path
from platform_contracts import ServingBinding, build_run_snapshot, load_catalog

catalog = load_catalog(Path.cwd())
snapshot = build_run_snapshot(
    catalog,
    "writer-assist-baseline@1",
    (
        ServingBinding(
            customer_ref="writer-assist@1",
            serving_profile_ref="gec-baseline@1",
            worker_group_ref="latency-workers@1",
            deployment_id="gec",
        ),
    ),
)
print(snapshot.scenario.ref)
print(snapshot.scenario.measurement_duration_s)
PY
```

This constructs a draft snapshot. `validate_for_execution(snapshot)` deliberately rejects it until
dataset artifacts, provider selections, version pins, an inventory snapshot, and an admission
schedule have been supplied. That preflight checks offline completeness, not live capacity or
deployment readiness.

### Files and generation

- [Python contracts and interface notes](packages/platform-contracts/README.md)
- [Customer defaults and scenario mappings](configs/customers/README.md)
- [Dataset provenance and preparation boundaries](datasets/README.md)
- [Runtime fixtures and schema conventions](contracts/README.md)
- [AWS access bootstrap and API DNS decisions](docs/runbooks/aws-access.md)

Author configurations under `configs/` and source manifests under `datasets/manifests/`. Generate
JSON Schemas from the Python models; do not edit the exported schemas by hand:

```bash
uv run --locked --package platform-contracts python scripts/export-contracts.py
```

CI runs the same checks and rejects schema drift. Management OpenAPI and a generated TypeScript
client will be added when the management API routes exist.

## AWS foundation

Stream B's Terraform foundation provides three continuously running CPU hosts in London for the
self-managed Kubernetes cluster. It includes native S3 state locking, imported access-bootstrap
IAM, a private API NLB, dedicated etcd disks and a separately authorized inference DNS root.
Ansible host preparation, direct WireGuard peers, guarded kubeadm bootstrap, Cilium/Hubble,
metrics-server, encrypted etcd backups and generic worker operations are implemented as a
separate rollout. [Live cluster acceptance](docs/runbooks/kubernetes-acceptance.md) passed,
including external API gateway failover, snapshot restore and generic worker join/removal.
Terraform does not provision Lambda GPU VMs.

```bash
make infra-tools
make infra-check
make infra-profiles
make infra-bootstrap-plan
make infra-preview
make infra-dns-preview ORIGINS_FILE=infra/terraform/environments/inference-dns/origins.example.tfvars.json
```

Preview plans cannot be applied. Review the [provisioning workflow](docs/runbooks/aws-foundation.md)
and its cost estimate before authorizing live creation. All mutable commands use explicit AWS
identities and saved-plan digests. Private credentials, recovery state and reports stay outside Git.

- [Foundation architecture and addressing](docs/architecture/aws-foundation.md)
- [State recovery](docs/runbooks/terraform-state.md)
- [Inference origin updates](docs/runbooks/inference-dns.md)
- [Ansible and Stream C inventory handoff](docs/runbooks/infrastructure-handoff.md)
- [Decommissioning and data deletion](docs/runbooks/foundation-teardown.md)
- [Live foundation acceptance and Ansible handoff](docs/runbooks/aws-foundation-acceptance.md)
- [Live Kubernetes acceptance and cleanup](docs/runbooks/kubernetes-acceptance.md)

## CPU cluster bootstrap

```bash
make cluster-tools
make cluster-check
make cluster-preflight
```

Review the [cluster rollout](docs/runbooks/cluster-bootstrap.md) before applying host changes.
The [network architecture](docs/architecture/kubernetes-bootstrap.md),
[backup recovery workflow](docs/runbooks/cluster-recovery.md) and
[worker operation interface for C](docs/runbooks/worker-host-operations.md) describe ownership,
credential boundaries and later GPU integration. Local validation is distinct from live
Kubernetes acceptance; no readiness is inferred from Terraform allocations.
