# Scripts

Run repository scripts from the root with the uv workspace and locked dependencies.

| Entry | Purpose |
| --- | --- |
| `export-contracts.py` / `check-generated.sh` | Generate/check JSON Schemas from shared Python contracts |
| `export-management.py` | Generate/check OpenAPI from real FastAPI routes |
| `smoke-workloads.py` | Prepare and run all seven CPU-only workload paths |
| `plot_a10_results.py` | Custom Matplotlib figures from published aggregate A10 results |
| `management/` | Application build/release, integration and explicit browser acceptance helpers |
| `infrastructure/` | Reviewed plans, pinned releases, host/cluster operations, readiness and recovery checks |

Infrastructure and opt-in browser scripts can mutate live resources or launch
benchmarks. Use their component runbooks and stable operation identities; do not
run every script as a generic cleanup command. Readiness and successful execution
are distinct from a passing SLO.

Raw data, captured responses, tokens, Terraform state and operational evidence
belong in ignored artifact/state storage, not in source or public logs.
