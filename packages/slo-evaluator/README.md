# SLO evaluator

Pure request/session classification and fixed-window evaluation over `platform_contracts`.
`evaluate(snapshot, requests, sessions)` returns the shared `RunResult`. It reconciles customer
counters, applies frozen targets, checks delivery validity, and calculates nearest-rank latency
summaries. Errors count against eligible attainment; warmup is excluded. Invalid measurements
produce no SLO assessments, and insufficient samples cannot pass.

This package has no HTTP, FastAPI, database, Kubernetes, provider SDK or AIPerf dependency.
