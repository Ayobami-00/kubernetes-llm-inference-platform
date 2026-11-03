# Customer examples

These five profiles are public-task-inspired experiments, not real customer configurations or
production traffic logs. Rates, limits, latency thresholds, reservations and hardware/model settings
are uncalibrated assumptions. A configured provisioned reservation expresses intent; it is not a
measured throughput entitlement until the platform is benchmarked and admission is implemented.

| Customer file | Inspiration | Serving profile | Worker group | Deployment ID |
| --- | --- | --- | --- | --- |
| `writer-assist.yaml` | Grammarly-like sentence correction | `gec-baseline@1` | `latency-workers@1` | `gec` |
| `code-agent.yaml` | Cognition/Devin-like coding calls | `code-agent-baseline@1` | `large-workers@1` | `code-large` |
| `tutor-chat.yaml` | MasterClass-like tutoring | `shared-small-baseline@1` | `shared-workers@1` | `shared-small` |
| `doc-flow.yaml` | LlamaIndex-like document work | `shared-medium-baseline@1` | `shared-workers@1` | `shared-medium` |
| `research-agent.yaml` | Parallel-like research substeps | `shared-medium-baseline@1` | `shared-workers@1` | `shared-medium` |

Public inspiration material:

- [Grammarly real-time inference talk](https://www.coreweave.com/resources/videos/how-grammarly-scales-real-time-inference-with-vllm-on-coreweave)
- [Cognition and CoreWeave](https://coreweave.com/news/coreweave-delivers-nvidia-vera-rubin-nvl72-performance-at-production-scale-starting-with-cognition)
- [MasterClass teaching agents](https://www.coreweave.com/news/coreweave-ai-cloud-platform-powers-masterclasss-ai-teaching-agents)
- [LlamaIndex conference session](https://www.coreweave.com/fully-connected-sessions/automating-document-work-with-long-horizon-ai-agents-at-llamaindex-413)
- [Parallel Web Systems talk](https://www.coreweave.com/resources/videos/beyond-the-benchmark-how-parallel-web-systems-builds-reliable-web-agents)

Conference participation or a workload analogy does not establish a particular customer's serving
topology. Public datasets are independent proxies; their source and limitations are recorded in
the dataset manifests.

## Experimental defaults

| Customer | Baseline admissions | Burst admissions (60 seconds) | Target | Minimum requests/window |
| --- | --- | --- | --- | --- |
| WriterAssist | 0.5 requests/s | 1.5 requests/s | 1,000 ms completion | 100 |
| CodeAgent | 1 session/min | 3 sessions/min | 2,000 ms TTFT | 20 |
| TutorChat | 6 sessions/min | 18 sessions/min | 1,000 ms TTFT | 20 |
| DocFlow | 0.25 requests/s | 0.75 requests/s | 30,000 ms completion | 20 |
| ResearchAgent | 2 sessions/min | 6 sessions/min | 2,000 ms TTFT | 20 |

Every target requires 95% attainment in each 300-second window. DocFlow's deadline is an internal
best-effort target, not a purchased capacity guarantee. CodeAgent loop duration and DocFlow batch
throughput belong in detailed runner measurements; TTFT or request completion alone does not
describe the entire workflow. Quality evaluation is separate from performance.

Each standalone scenario has 60 seconds of unscored warmup followed by 180 seconds steady traffic,
60 seconds at the explicit burst rate, 360 seconds recovery, then up to 120 seconds drain. Seed is
42. Inter-request and session-start admissions are Poisson; within a session, turns wait for the
previous response. These are configurations only, not materialized schedules or completed runs.

`shared-contention` combines TutorChat, DocFlow and ResearchAgent. `platform-mixed` includes all
five. The two medium-model customers deliberately share a deployment; giving them different
deployment IDs would request two sets of medium replicas and exceed the four-GPU shared group.

The shared profiles each have two one-GPU replicas on the same four-GPU worker. This does not
demonstrate resilience to losing that host. The large model's eight-GPU candidate has not been
validated for compatibility, loading or memory headroom. Both are future experiment concerns.

WriterAssist warms up using JFLEG development data and scores against test data. Other datasets
must reserve disjoint warmup records during preparation. No example is currently executable:
provider regions/SKUs, artifact pins and prepared datasets remain unresolved.
