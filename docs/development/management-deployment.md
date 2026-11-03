# Management deployment and release

The local Compose workflow remains `./dev/start`. Production images use immutable source, a
non-root Python API/worker runtime and compiled web assets served by nginx. Vite and development
reload are confined to explicit development targets. API and worker share the backend image.
The build stages declared uv workspace members (including root-level runner/stub packages) from
the filtered context, and installs only `platform-management` and its dependencies. A's runner
dependencies are not installed into the management image merely because they share the workspace.

## Charts and prerequisites

C owns `charts/klip` and `charts/klip-postgresql`. B references these charts from its static release
tooling; do not create a second owner for either release. They are separate releases so PostgreSQL
can become ready before the application's pre-install/pre-upgrade migration hook.

The database chart supplies a single PostgreSQL 17.11 primary with a digest-pinned image, a retained
RWO PVC, a dedicated non-superuser application role and ClusterIP service. This is persistent storage,
not database HA. Backups and storage failure recovery remain necessary.

Before installation, B supplies:

- Kubernetes 1.31+ on stable CPU nodes, a namespace for management and the serving namespace.
- A PostgreSQL RWO storage class or existing PVC; a real multi-node RWX artifact claim/storage class.
- Existing Secrets: database `postgres-password`/`app-password`, a matching application `database-url`,
  and authentication `admin-password`/`lambda-api-key` (the latter may be empty).
- GHCR pull credentials when packages are private, and TLS/origin/ingress configuration for exposure.

Create Secrets from private files or an external secret manager. The application chart references
existing Secrets and never emits secret contents into Helm values. Do not commit credentials, use
command-line password literals or grant the application AWS permissions. All application Pods run
on CPU control-plane nodes by default; placement is configurable in the values files.

An example application values file (replace every example with actual supplied identifiers):

```yaml
backend:
  image: ghcr.io/OWNER/REPOSITORY/management-backend@sha256:BACKEND_DIGEST
web:
  image: ghcr.io/OWNER/REPOSITORY/management-web@sha256:WEB_DIGEST
auth:
  existingSecret: klip-auth
database:
  existingSecret: klip-db-url
publicOrigin: https://klip.example.org
artifacts:
  existingClaim: klip-artifacts
runner:
  simulationAdapter: simulated:v1
  realAdapter: unconfigured:v1
  servingNamespace: llm-d
ingress:
  enabled: true
  className: nginx
  host: klip.example.org
  tlsSecret: klip-tls
```

Keep the deployment private until TLS and the exact allowed origin are configured. Secure session
cookies are the default. `localDevelopment: true` is solely for explicit local HTTP testing.
`runner.realAdapter` remains unconfigured until the handoff and readiness checks are satisfied.

For charts supplied locally, install in this order:

```bash
helm upgrade --install klip-db charts/klip-postgresql -n klip \
  --set auth.existingSecret=klip-db-credentials \
  --set persistence.storageClass=YOUR_RWO_CLASS --wait --timeout 5m
helm upgrade --install klip charts/klip -n klip -f /private/path/management-values.yaml \
  --wait --timeout 5m
```

The application hook serializes Alembic migration and idempotent seed initialization before new
API/worker Pods start. It uses the existing database secret and the same backend image, with no
artifact or application-service dependency. Normal upgrades preserve published revisions and
history. Take and verify a backup before schema upgrades; application rollback does not reverse a
migration. Never use automatic database downgrade as a rollback strategy.

Worker RBAC permits management-namespace Jobs, Pod reads, node reads and read-only serving
Deployment observations. Runner Pods have no service-account token. Management does not create
Nodes, run host bootstrap, mutate static releases or allocate real cloud instances in this milestone.

## Isolated Kubernetes proof

Prerequisites: Docker with sufficient disk space (at least 10 GiB free), kind 0.30.0, Helm 3.17.1,
kubectl, Python 3, Node 24, `npm ci` and Playwright Chromium installed in the web directory.

```bash
./dev/test-integration
```

The command builds production images, creates a private kubeconfig and a uniquely named CPU-only
kind cluster, installs both charts, imports synthetic records and a checksummed artifact, runs a
real Kubernetes Job carrying synthetic results, replaces workers, and checks that the same Job UID
and one runner invocation survive. Browser checks use the real API/database. It then upgrades the
application, restores a logical database backup to a separate database, and replaces the database
Pod to check PVC persistence. It deletes only its own cluster and private temporary files on exit.
No Lambda key, existing kubeconfig or connected local application is used.

Use `KLIP_KEEP_INTEGRATION=1 ./dev/test-integration` only when debugging. The printed directory contains
private test credentials: do not publish it. Remove the named cluster and private directory after
inspection. CI runs this proof as a release gate alongside the existing Compose browser checks.
A failed proof is not evidence that any real inference integration works.

## Explicit release tags

`.github/workflows/management-release.yaml` publishes only on a pushed `management-vX.Y.Z` tag.
Ordinary branches and main commits run verification without publication. The tag must reference a
commit on main; backend, web and both chart versions must match. The workflow first runs all management
checks, then builds amd64/arm64 images, publishes to GHCR, and packages OCI charts with the exact image
index digests in their default values. `release.json` records version, commit, image digests and charts.
Publishing this workflow is not permission to create a tag or deploy a release automatically.

Artifacts after an explicitly authorized release:

- `ghcr.io/OWNER/REPOSITORY/management-backend:X.Y.Z`
- `ghcr.io/OWNER/REPOSITORY/management-web:X.Y.Z`
- `oci://ghcr.io/OWNER/REPOSITORY/charts/klip` at version `X.Y.Z`
- `oci://ghcr.io/OWNER/REPOSITORY/charts/klip-postgresql` at version `X.Y.Z`

B pins chart versions and uses the recorded image digests. GHCR access/visibility is controlled by
repository/package settings; private pulls need an existing imagePullSecret. The workflow logs in
with its scoped `GITHUB_TOKEN`; no registry token belongs in source or chart values.

Validate versions or package locally without publishing:

```bash
uv run --locked --package platform-management python scripts/management/release.py \
  --tag management-v0.2.0
# To package: also pass --repository OWNER/REPOSITORY, --backend-digest sha256:...,
# --web-digest sha256:..., and --commit COMMIT. Outputs go to ignored artifacts/releases.
```

Official references: [Helm hooks](https://helm.sh/docs/topics/charts_hooks/),
[Helm OCI registries](https://helm.sh/docs/topics/registries/),
[Kubernetes Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/), and
[kind image loading](https://kind.sigs.k8s.io/docs/user/quick-start/#loading-an-image-into-your-cluster).
