# Workload runner

Preparation, immutable scenario compilation, native AIPerf 0.13.0 execution, quality evaluation
and reports for WriterAssist, CodeAgent, TutorChat, DocFlow and ResearchAgent.

See [the development guide](../docs/development/workloads.md) for CPU-only startup, all seven
scenarios, public data, CLI commands, artifacts and containers; see
[the integration notes](../docs/development/workload-integration.md) for C's runner protocol.

`plan` also emits a management import bundle. `submit` launches that bundle through C and collects
verified results; `execute --manifest` is the execution-only entrypoint used inside C's runner Jobs.
Use `plan --sustained` for a derived 20-minute workload with unchanged performance targets. Live
execution requires B's real pinned deployment and capacity handoff; follow the
[live validation runbook](../docs/runbooks/live-inference-validation.md).

The benchmarking and data dependencies are optional extras. Importing shared contracts or the
pure SLO evaluator does not import AIPerf, a service framework, or a cloud client.
