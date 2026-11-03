# Management application

Stratum is the local Stream C management application. Its graph connects dataset manifests,
customer workloads, serving assignments, worker specifications, simulated allocations and runs.
Select a resource to inspect or edit it in the right-hand panel. Lists provide another way to browse.

## Start from a clean checkout

Requirements: Docker Desktop (or Docker Engine with Compose v2), sufficient Docker build memory,
and the repository checked out on the management branch. Host Python and Node are unnecessary
for the Docker workflow.

From the repository root:

```bash
./dev/start
```

On first use this prompts for the password for **admin**, creates private ignored secret files,
builds the images, starts PostgreSQL, applies Alembic migrations and seeds examples. Open
[http://localhost:5173](http://localhost:5173). Subsequent starts preserve the password and database.
PostgreSQL 17.11 is pinned by image digest. API and worker share the backend image and package.

The example catalogue includes five customers, seven scenarios, three worker-group specifications,
four serving profiles and six dataset manifests. Imported historical examples are synthetic fixtures.
Initialization is serialized and repeatable; it never replaces edited specifications or launches jobs.

Stop without deleting data:

```bash
docker compose -f dev/compose.yaml down
```

Normal restarts preserve the named PostgreSQL volume. Avoid `down -v` unless intentionally
resetting all local management data. The web and API development containers reload source changes.
Restart the worker after changing its source:

```bash
docker compose -f dev/compose.yaml restart worker
```

## Secrets and authentication

`dev/.secrets/admin_password` supplies the fixed `admin` account. There is no default password.
Sessions use HttpOnly, SameSite cookies and expire after eight hours. Mutations require CSRF tokens.
Five failed logins from the same address within ten minutes temporarily block further attempts.

To enable live Lambda reads, place the API key in `dev/.secrets/lambda_api_key`. Keep file mode 600.
The worker checks configuration on each refresh; refreshes are coordinated across replicas.
The key is never sent to the browser. API documentation is at `/api/docs` on the backend port 58000.

Live integration reads instance types, prices, reported regional availability and account instances.
It has no allocation, termination, AWS or DNS implementation. All application lifecycle actions in
this milestone are simulated, including preparation, cluster joining and serving deployment.
Account instances are observational and are not adopted into managed groups.

Local HTTP uses non-Secure cookies. Before external deployment, configure TLS, the exact allowed
origin and `MANAGEMENT_SECURE_COOKIES=true`, and complete the external deployment review.
`dev/.env.example` documents backend settings; Compose supplies its own container settings.
The documented PostgreSQL password is a loopback-only development credential.

## First end-to-end workflow

1. Open Topology and select WriterAssist to inspect its workload and SLO requirements.
2. Select the latency worker specification. Create a simulated group with a unique stable name.
3. Observe allocation, preparation, joining and GPU readiness in Operations. The lease begins at
   the first confirmed allocation, including preparation time.
4. In Simulation lab select the Qwen3-4B serving node, choose the ready group and deploy it.
5. Select Writer Assist Baseline, choose a result preset and launch a simulated run.
6. Open Runs & results. Inspect execution, measurement validity, SLO assessments and unevaluated
   quality separately. Explore the frozen graph from the run inspector.
7. Reload or restart containers. Published revisions, resources, operations and results persist.

To exercise the full mixed scenario, create all three groups and deploy all four serving profiles.
DocFlow and ResearchAgent share **one** `shared-medium` deployment. Configured demand is 13 GPUs;
this is configuration intent, not observed or guaranteed capacity.

Group creation presets include shortage, transient failure, partial allocation/bootstrap failure,
ambiguous allocation and termination failure. The run presets include passing, violating,
insufficient data, invalid measurements and execution failure. These are deterministic fixture
builders, not benchmarks or an alternative SLO evaluator. Synthetic runs use compressed wall time;
their snapshot measurement windows retain the authored durations. No quality scores are claimed. Double-click a runtime group in the graph to expand or collapse its GPU instances.

Real-run preflight remains blocked by draft configurations and unavailable integrations. Publishing
configuration does not assert that datasets, images, hardware compatibility or endpoints are ready.

## Configuration and export

Save a draft, then publish a new immutable revision. Customer/scenario/profile/group editing supports
common fields and an advanced JSON editor. API validation is authoritative. Conflicting saves require
reloading. Existing assignments retain exact revisions; update a customer's assignment explicitly
when moving to new published inputs. Historical runs retain independent immutable snapshots.

Export a published resource as YAML from its inspector, or export the catalogue ZIP from navigation.
PostgreSQL is authoritative after seeding; the application never edits repository configuration YAML.
Dataset manifests are supplied by A and are view-only in this UI.

## Development and verification

Host development uses Python 3.12 with uv and Node 24 with npm:

```bash
uv sync --locked --all-packages
uv run --locked --package platform-management alembic -c apps/management/backend/alembic.ini upgrade head
uv run --locked --package platform-management management-init
uv run --locked --package platform-management pytest packages/platform-contracts/tests apps/management/backend/tests
uv run --locked --package platform-management ruff check .
uv run --locked --package platform-management ruff format --check .
bash scripts/check-generated.sh
uv run --locked --package platform-management python scripts/export-management.py --check
cd apps/management/web
npm ci
npm run generate
npm run format:check
npm run build
npm test
npx playwright install chromium
npm run test:e2e
```

Backend integration tests create/reset a separate `management_test` database on the local development
PostgreSQL service. Override `MANAGEMENT_TEST_DATABASE_URL` to use another test server; the database
name must end in `_test`. Tests never truncate the application database.
Browser tests use the running Docker application and read its local secret file, or accept
`MANAGEMENT_E2E_PASSWORD`. They create disposable simulated resources and publish test revisions.
Browser tests do not contact Lambda. Screenshots are written under ignored `artifacts/management/`.

Regenerate API definitions after route/model changes:

```bash
uv run --locked --package platform-management python scripts/export-management.py
cd apps/management/web
npm run generate
```

The committed OpenAPI is generated from real routes; the SDK and isolated MSW mocks use its types.
CI checks contract/schema/OpenAPI drift, generated client drift, Python tests, frontend checks and
browser workflows against the complete Docker stack.

## Downstream interfaces

A supplies real runner launch/stop/observe integration, prepared artifacts, measurements and validated
RunResult records. C consumes RunSnapshot and RunResult and performs completeness/readiness checks.
A lost real execution attempt must be observed or failed, never silently restarted with a new clock.

B supplies InfrastructureInventory and reusable preparation, join and removal automation. C's
adapter interfaces use credential references rather than embedding secrets. B owns static releases;
C owns future dynamic model-pool releases and its eventual management Helm chart. There is one owner
per release. AWS lifecycle, DNS and firewall foundation changes remain B's responsibility.

The TypeScript generator consumes the committed JSON OpenAPI document. A patched js-yaml override
keeps its optional YAML parser dependency current; the generated client is checked after regeneration.

## Verification record

Verified locally on 3 November 2026:

- 171 Python tests, including the original contract suite and management integration tests.
- Nine frontend presentation tests and successful production builds on host and Node 24 Docker.
- Ten browser tests cover both appearances, system preference changes, readable configuration/history,
  form/advanced editing, draft publication, keyboard interaction and mobile layouts. Automated WCAG
  checks cover login, topology, customers, capacity, results, operations and simulation. The complete
  allocation/worker-restart/serving/run/history workflow and insufficient-data navigation also pass.
- Alembic upgrade/downgrade/upgrade, repeatable seeding, concurrent claims, stale owners, ambiguous and
  unresolved allocation outcomes, termination failure, expiry during preparation and serving replacement.
- Ruff lint/format, contract schemas, management OpenAPI and deterministic generation of 16 SDK files.
- Docker startup and matching API/worker image identity; npm audit reported no vulnerabilities.

Automated Lambda checks use HTTP mocks for read-only requests, safe payload persistence, refresh
errors and stale observations. UI tests use an isolated account integration with an empty credential,
preserving the connected workspace and its live observations. Real runner,
bootstrap, Kubernetes and dynamic Helm integration remain the downstream dependencies described above.

## Appearance and readable specifications

Use the sun, moon or monitor controls in the top bar (also available on the login page) to select
light, dark or system appearance. The browser remembers the preference across reloads and sign-ins;
system appearance follows OS changes. The graph, inspector, forms and result states share the theme.

The inspector presents specifications as expandable sections with labels, units, prompt text,
traffic phase summaries, provenance and preparation status. Revision history uses the same readable
presentation. Raw JSON is available only through the advanced configuration editor. Switching back
to the form preserves valid JSON changes; save the draft before publishing further edits.

For browser tests alongside a connected workspace, use the isolated Docker test command:

```bash
# Prerequisites: Node 24, npm ci in apps/management/web, and Playwright Chromium installed.
./dev/test-ui
```

This starts a disposable application on localhost:5174 with its own database, image names and secrets.
Its Lambda key is empty. It builds the production bundle and runs frontend tests in Node 24 Docker,
then tests both themes, system preference changes, structured specifications,
editing, keyboard interaction, desktop/mobile layouts and automated WCAG accessibility rules, plus
the complete simulated lifecycle workflow. Its containers and test volume are removed on exit.
Ports 5174, 58001 and 55433 must be free. Docker Compose 2.24.4 or newer is required for the override.
Screenshots are saved under `artifacts/management/`. The connected workspace and its secrets are kept
separate. To run one case, pass Playwright arguments, e.g. `./dev/test-ui --grep accessibility`.
