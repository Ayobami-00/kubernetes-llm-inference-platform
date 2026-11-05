# Contributing

Use Python 3.12 and uv for the workspace; the frontend uses Node 24 and npm.
Start with the [repository map](README.md#repository-map) and
[development documentation](docs/README.md). Keep changes focused on the owning component.

## Python and generated interfaces

```bash
uv sync --locked --all-packages
uv run --locked --all-packages pytest
uv run --locked --all-packages ruff check .
uv run --locked --all-packages ruff format --check .
bash scripts/check-generated.sh
uv run --locked --all-packages python scripts/export-management.py --check
```

The root suite is offline; some integration tests are opt-in and need local
sockets or pinned tools. PostgreSQL-backed management tests use a **dedicated
database ending in `_test`** via `MANAGEMENT_TEST_DATABASE_URL`. Never point tests
at a live management database. See [management development](docs/development/management.md).

Edit shared Python models and real API routes first, then regenerate their JSON
Schemas/OpenAPI. Generate the frontend client with `npm run generate` in
`apps/management/web/`. Include intentional generated changes with their source.
Do not hand-edit generated clients or rewrite published snapshots/assessments.

## Frontend

From `apps/management/web/`:

```bash
npm ci
npm run generate
npm run format:check
npm test
npm run build
```

`./dev/test-ui` runs the isolated browser workflow. The full CPU Kubernetes proof
is `KLIP_WORKLOAD_INTEGRATION=1 ./dev/test-integration`. These require the tools
described in the development guides and remain separate from live GPU acceptance.

## Experiments and documentation

```bash
uv run --locked --group plots python scripts/plot_a10_results.py
```

Publish aggregates, methods, pins and limitations. Keep raw corpora, references,
captured responses and credentials private. Synthetic results must stay labelled;
an execution completing does not imply valid delivery, quality or passing SLOs.
Preserve the original result when reassessing an experiment.

Check Markdown links and inspect generated figures before submitting. Explain
what changed and the relevant verification; disclose live gates that were not run.
Cloud provisioning, dataset downloads and infrastructure destruction are explicit
operator actions, not contributor setup steps.

## Security and licensing

Follow [SECURITY.md](SECURITY.md). Do not attach unredacted plans, kubeconfigs,
logs or private experiment bundles to public issues. Code uses [Apache-2.0](LICENSE);
data, weights and upstream artifacts keep their own licenses.
