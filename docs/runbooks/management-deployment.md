# Management deployment on persistent CPU workers

The release uses two API, worker and web replicas on the three protected CPU
workers, the migrated CloudNativePG database and the retained RWX artifact claim.
It enables actual Kubernetes runner Jobs, with fixture-labelled execution through
the workload stub adapter and strict real-run preflight. Provider, host, cluster
and serving mutation adapters remain explicitly unconfigured until their real
integration is complete; deployment cannot rent a Lambda instance implicitly.

## Credentials and release inputs

```sh
make management-init-admin
make management-stage
make management-plan RECEIPTS=<downloaded-private-image-receipt-directory>
make management-deploy PLAN_SHA=<reviewed-release-plan-digest>
```

Initialization creates a strong credential only when no local admin file or live
management identity exists. It never prints the value or replaces an existing
credential. The initial deployment credential is stored in the ignored private
`dev/.secrets/admin_password` file. The provider key remains in its existing
`dev/.secrets/lambda_api_key` reference. Staging compares retained PostgreSQL
credentials with the live database and creates immutable, namespaced application
Secrets. It copies only the public database CA, with no CA signing key.

The stage receipt binds namespace/database identities and Secret UIDs. Changed
values or missing previously observed Secrets require explicit rotation/recovery.
Unfinished legacy jobs block first startup. Later upgrades preserve active work.
The dedicated namespace enforces restricted Pods at Kubernetes 1.36; the application
uses non-root users, RuntimeDefault seccomp, dropped capabilities and no privilege
escalation. API, worker, web and migration root filesystems are read-only. Database
connections require verified TLS and the non-superuser management role.

Preparation checks that executable/configuration inputs match the CI image source,
while allowing subsequent infrastructure changes. It verifies the scan receipts,
renders the complete chart and checks runtime controls before freezing values,
source/manifest hashes, node identities and Secret references. The private
`cluster/management/plan.json` is the reviewable deployment input. Deployment
rechecks these inputs and uses atomic Helm installation/upgrade with a ten-minute
deadline. A Helm success establishes deployment, not HA or live-inference acceptance.

Preserve the admin/provider credentials in encrypted external custody before the
retained-state destroy/rebuild rehearsal. Database-secret custody is already a
separate verified procedure; it does not cover these two application credentials.

## HTTPS and certificate renewal

The lab overlay enables native HTTPS on the web Service's port 8443. cert-manager
owns a namespace-local internal CA and a 90-day certificate, renewed 30 days before
expiry. Each certificate key rotates. Nginx loads the mounted certificate through
a constant mapped basename and refreshes its certificate cache within 30 seconds.
Client-controlled SNI cannot select another file. This uses the
[supported Nginx variable-certificate cache](https://nginx.org/en/docs/http/ngx_http_ssl_module.html#ssl_certificate_cache)
without an extra watcher, API permissions or shared process namespace. Verify
actual renewal/handshakes before claiming this behavior is live-proven.

The HTTPS Service is initially ClusterIP. `https://management.klip-lab.click` is its
intended origin; a resilient external frontend and public certificate/DNS remain a
separate deployment step. Internal probes must verify the internal CA and hostname.
The generic chart ingress supports its existing TLS-termination mode with native
web TLS disabled. It rejects combining that HTTP-upstream configuration with the
native HTTPS mode. Keep the current private API/SSM access path for operator work.

## Image review

The first old image pins had critical findings and were held back. Python 3.12.16
and the Nginx 1.30.6 slim runtime removed those critical findings in the subsequent
scans. Remaining findings are retained with their original severities. The
versioned `deploy/image-risk-review.json` expires on 2026-12-03 and applies only to
the declared cluster runtime, with exact package versions and controls. It does
not authorize arbitrary scripts, host execution or workstation dataset preparation.
Critical, unreviewed, stale, incomplete or mismatched image scans fail preparation.

| Finding group | Relevant condition and runtime boundary | Vendor evidence |
| --- | --- | --- |
| util-linux mount/probing issues | No privileged mount or block-device processing path; non-root, no capability additions, no privilege escalation or host/device mounts | [bind mounts](https://security-tracker.debian.org/tracker/CVE-2026-78410), [mount targets](https://security-tracker.debian.org/tracker/CVE-2026-53613), [partition probing](https://security-tracker.debian.org/tracker/CVE-2026-53615) |
| SQLite FTS/session issues | PostgreSQL management storage; runtime code does not accept SQLite files, changesets or SQL/FTS inputs | [FTS prefix queries](https://security-tracker.debian.org/tracker/CVE-2026-119373), [FTS deletion](https://security-tracker.debian.org/tracker/CVE-2026-119374), [changesets](https://security-tracker.debian.org/tracker/CVE-2026-119379) |
| glibc locale/catalog issue | C locale frozen in process configuration; request content cannot set locale variables or supply catalogs | [vendor conditions](https://security-tracker.debian.org/tracker/CVE-2026-84243) |
| zlib non-blocking writer issue | No non-blocking gzwrite/gzprintf/gzvprintf sequence in this runtime path | [vendor conditions and affected-range discussion](https://security-tracker.debian.org/tracker/CVE-2026-85091) |

These are scoped applicability/mitigation assessments, not claims that vulnerable
packages were patched or that findings disappeared. Reassess when inputs,
dependencies or privileges change, and before the review expires. Source and
rendered runtime checks support the assessment; actual deployed Pods still need
inspection. Keep the complete scan receipts alongside the release evidence.

## Remaining live acceptance

Verify certificate issuance/rotation, all replicas' placement and runtime
configuration, authenticated reads/writes, EFS access and PostgreSQL connections.
Then test API/worker loss and PostgreSQL failover using the objectives in
`docs/architecture/platform-availability.md`. Record recovery times, Job UIDs,
execution timelines and partial artifacts. The external frontend, real Lambda
lifecycle, inference stack, telemetry and retained-state rebuild remain separate
required gates; this release procedure does not establish them.
