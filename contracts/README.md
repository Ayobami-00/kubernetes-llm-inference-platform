# Schemas and runtime fixtures

`schemas/` contains twenty-one self-contained JSON Schema 2020-12 documents generated from the Python
models, including execution targets, preparations, admissions, requests, sessions and runner
observations and typed host-executor tasks/observations. Python models are authoritative.
`additionalProperties: false` rejects unknown fields;
cross-record, timestamp-ordering and arithmetic rules are additionally enforced by Pydantic and
the shared validators, because JSON Schema alone does not encode all those checks.

`fixtures/` contains 34 synthetic runtime records:

- Five customer folders each have queued, completed passing, completed violating and insufficient-
  data runs. Each run includes its resolved draft snapshot and has `fixture: true`.
- Two queued mixed-scenario runs show the complete assignment chains.
- Three execution cases illustrate failure, cancellation and invalid measurements.
- Five worker-group cases cover joining, ready, partial failure, manual deletion and expired/terminated capacity.
- Three operation cases cover running, completed and failed actions, including pending cleanup.
- One inventory contains three AWS CPU control-plane nodes and three representative Lambda workers.

Counts, token totals and latency quantiles are fabricated test values; they do not correspond to a
seed-42 replay, an AIPerf export, real capacity, measured quality or a customer production trace.
`fixture://` artifacts intentionally do not exist. Prepared dataset checksums remain unset, and no
quality scores are claimed. Documentation IP ranges and example hostnames identify synthetic hosts.

The test suite validates every fixture with both its Python model and generated JSON Schema. It
also checks authored examples, reference resolution, snapshot independence, request accounting,
SLO consistency and completeness preflight. Historical snapshots need not be rewritten when a
new configuration revision is introduced.

From the repository root:

```bash
uv run --locked --package platform-contracts python scripts/export-contracts.py
bash scripts/check-generated.sh
```

The check command never rewrites files. It fails on missing, changed or obsolete schema exports.
`management.openapi.json` is generated from the real FastAPI routes. The web application generates
its TypeScript SDK from that document and CI checks both outputs for drift.
