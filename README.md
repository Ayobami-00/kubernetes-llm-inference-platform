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

    P["Persistent CPU environment<br/>Kubernetes control plane, platform controller,<br/>observability and experiment results"]
    P -. configuration and lifecycle .-> G
    P -. configuration and lifecycle .-> R
    P -.-> L
    P -.-> A
    P -.-> B
    P -.-> S
```
