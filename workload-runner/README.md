# Workload runner

Preparation, immutable scenario compilation, native AIPerf 0.13.0 execution, quality evaluation
and reports for WriterAssist, CodeAgent, TutorChat, DocFlow and ResearchAgent.

See [the development guide](../docs/development/workloads.md) for CPU-only startup, all seven
scenarios, public data, CLI commands, artifacts and containers; see
[the integration notes](../docs/development/workload-integration.md) for C's runner protocol.

The benchmarking and data dependencies are optional extras. Importing shared contracts or the
pure SLO evaluator does not import AIPerf, a service framework, or a cloud client.
