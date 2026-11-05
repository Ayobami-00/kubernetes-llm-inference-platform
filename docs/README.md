# Documentation

Start with the [project overview and measured A10 result](../README.md). This index
separates development instructions, deployment procedures and dated evidence.

## Develop locally

- [Management UI, API and PostgreSQL](development/management.md)
- [Workload preparation, execution and evaluation](development/workloads.md)
- [A↔C workload integration](development/workload-integration.md)
- [Durable application integrations](development/management-integrations.md)
- [Contributing and verification](../CONTRIBUTING.md)

## Understand the platform

- [AWS foundation](architecture/aws-foundation.md)
- [Kubernetes and host networking](architecture/kubernetes-bootstrap.md)
- [Availability, durability and recovery targets](architecture/platform-availability.md)
- [Shared contracts](../packages/platform-contracts/README.md)
- [Customer profiles](../configs/customers/README.md)
- [Repository component map](../README.md#repository-map)

## Deploy and operate

- [AWS access](runbooks/aws-access.md) and [foundation provisioning](runbooks/aws-foundation.md)
- [Cluster bootstrap](runbooks/cluster-bootstrap.md) and [CPU workers](runbooks/platform-workers.md)
- [Management deployment](development/management-deployment.md)
- [CloudNativePG](runbooks/management-postgresql.md), [storage](runbooks/platform-storage.md)
  and [workload identity](runbooks/workload-identity.md)
- [Restricted worker-host operations](runbooks/worker-host-operations.md)
- [GPU Operator](runbooks/gpu-operator.md) and [runtime security](runbooks/gpu-runtime-security.md)
- [Telemetry releases and alerting](runbooks/platform-observability.md)
- [Private operator access](runbooks/platform-access.md)
- [Retained A10 serving](runbooks/retained-a10-speculation.md)
- [On-demand dashboard comparison](runbooks/dashboard-a10-comparison.md)
- [Workload recovery](runbooks/workload-recovery.md), [cluster recovery](runbooks/cluster-recovery.md)
  and [retained-state teardown](runbooks/foundation-teardown.md)

Runbooks include project-specific deployment inputs. Commands that mutate resources
require the documented identities, reviewed plans and retained state. Reading a
runbook does not authorize executing it against someone else's infrastructure.

## Experiments and acceptance

- [4 November 2026: real dashboard-driven A10 comparison](experiments/writer-assist-a10-dashboard-2026-11-04.md)
- [Experiment specification](../experiments/writer-assist-a10/ngram-comparison.md)
- [Aggregate results and reproducible Matplotlib figures](../experiments/writer-assist-a10/README.md)
- [Live CPU-cluster acceptance](runbooks/kubernetes-acceptance.md)
- [Dated delivery history and remaining gates](development/end-to-end-platform.md)

Acceptance records describe the tested versions and environment at the recorded
time. They are not a live-health indicator. Local synthetic tests, public-input
stub tests and real GPU performance evidence have different scopes.
