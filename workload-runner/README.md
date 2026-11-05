# Workload runner

Preparation, immutable scenario compilation, native AIPerf 0.13.0 execution, quality evaluation
and reports for WriterAssist, CodeAgent, TutorChat, DocFlow and ResearchAgent.

See [the development guide](../docs/development/workloads.md) for CPU-only startup, all seven
scenarios, public data, CLI commands, artifacts and containers; see
[the integration notes](../docs/development/workload-integration.md) for C's runner protocol.

`plan` also emits a management import bundle. `submit` launches that bundle through C and collects
verified results; `execute --manifest` is the execution-only entrypoint used inside C's runner Jobs.
Use `plan --sustained` for a derived 20-minute workload with unchanged performance targets. Live
execution requires a pinned serving deployment and capacity handoff; follow the
[live validation runbook](../docs/runbooks/live-inference-validation.md).

The benchmarking and data dependencies are optional extras. Importing shared contracts or the
pure SLO evaluator does not import AIPerf, a service framework, or a cloud client.

The [recorded WriterAssist A10 comparison](../experiments/writer-assist-a10/README.md)
uses this same managed execution pipeline, with real public JFLEG inputs and
separate immutable baseline/n-gram runs. Other customer paths have local pipeline
coverage; the A10 result does not validate their live model capacity.
