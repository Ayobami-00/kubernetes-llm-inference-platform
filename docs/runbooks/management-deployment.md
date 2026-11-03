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
make management-verify
make management-evidence
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

```sh
make management-verify-renewal OPERATION=<stable-12-character-hex-id>
```

This bounded test triggers the same manual issuance status transition as the pinned
[upstream cmctl command](https://github.com/cert-manager/cmctl/blob/v2.6.1/pkg/renew/renew.go),
adding UID/resourceVersion preconditions and durable submission intent. An ambiguous
submission is reconciled rather than blindly repeated. It reads only public
certificates, requires a new leaf public key with an unchanged CA, verifies TLS
handshakes against each exact web Pod and confirms unchanged Pod UIDs/restart counts.
It then authenticates again through the HTTPS Service. A successful forced renewal
proves operator issuance and mounted-certificate propagation; it does not measure
a complete 90-day automatic renewal interval or ACM public-certificate renewal.

The lab HTTPS Service uses NodePort 30443 for the dedicated external frontend.
Internal probes must verify the internal CA and hostname.
The generic chart ingress supports its existing TLS-termination mode with native
web TLS disabled. It rejects combining that HTTP-upstream configuration with the
native HTTPS mode. Keep the current private API/SSM access path for operator work.

## Public frontend and activation

Terraform root `platform-frontend` owns the management ALB, WAF, ACM certificate,
certificate-validation record and management DNS alias. It uses the three existing
CPU subnets and instance targets, with encrypted HTTPS to NodePort 30443. Only the
ALB security group can reach that port through the worker security group. The
public listener accepts HTTPS 443, uses the reviewed TLS 1.2/1.3 policy and forwards
only the management Host header. Its default action is 404. There is no public
HTTP listener or Kubernetes API access. Deletion protection is enabled.

The ALB does not validate backend certificates, as documented by
[AWS HTTPS target groups](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-target-groups.html).
This is encrypted transport inside the security-group boundary, not mutually
authenticated TLS. Internal operator probes independently verify the backend CA.
ACM manages the public certificate using its retained DNS-validation record.
ALB access-log storage remains a telemetry milestone; native ALB/WAF metrics are
available. WAF sampling is disabled to avoid retained request bodies or headers.

The two WAF IP rules allow 3,000 total requests and 30 login requests per five-minute
window before returning 429. Application login throttling remains stricter. Nginx
trusts forwarded addresses only from the ALB subnets and the three NodePort source
addresses. It discards the incoming chain after resolving the client address. The
API trusts only Pod-CIDR proxies, and an ingress NetworkPolicy restricts API access
to this release's web Pods. Do not disable the policy while retaining proxy trust.

```sh
make management-frontend-cost
make management-frontend-plan
make management-frontend-apply PLAN_SHA=<reviewed-unpublished-plan>
make management-frontend-verify
make management-frontend-activate
make management-frontend-plan
make management-frontend-apply PLAN_SHA=<reviewed-dns-activation-plan>
```

The first apply leaves public management DNS absent. Verification requires all three
exact instance targets healthy, correct live project ownership and authenticated
HTTPS using the public certificate/hostname. Before DNS exists it connects to the
ALB address while retaining management SNI and Host, without disabling TLS checks.
Activation records durable intent bound to the current ALB/target-group/instances;
planning and applying publication repeat the fresh checks. A rebuilt allocation
cannot inherit stale activation intent. Cookies and CSRF tokens stay in memory,
and the probe logs out its own session.

Deactivation records unpublished intent. Review a subsequent `manage.py plan --root
frontend --allow-origin-removal`, then apply that exact saved plan. Only removal of
the management alias is permitted by this override. It cannot delete an ALB or any
other resource. Full teardown remains a separate retained-state procedure.

The 2026-11-03 AWS Price List estimate is $43.40/month at 730 hours and one average
LCU: ALB $0.02646/hour, LCU $0.0084/hour, three public IPv4 addresses
[$0.005/hour each](https://aws.amazon.com/vpc/pricing/), plus
[WAF](https://aws.amazon.com/waf/pricing/) $5/ACL and $1/rule monthly. Request volume,
extra LCU/IP usage, data transfer, DNS queries and tax are additional. No new VM or
GPU is included. Keep the generated cost receipt with the saved infrastructure plan.

After publication, `make management-public-ui-verify` checks real browser
authentication, the topology/inspector and retained run history through the public
certificate. It takes only post-login screenshots and exports no trace, video,
HAR, cookies or browser storage. Its default operation makes no resource mutations.

`make management-verify-api-recovery OPERATION=<stable-12-hex-id>` additionally
restarts one exact API Pod while independently probing authenticated session reads
through the public frontend. It verifies the active namespace/release, deployment
and ReplicaSet ownership, two spread Ready replicas and healthy PostgreSQL before
submitting a UID-conditional deletion. It waits for two spread replacement replicas.
An interrupted operation observes its original target rather than deleting another
Pod. Recorded probes use a one-second interval and two-second timeout; an outage
shorter than the sampling interval can go unobserved. The recovery upper bound
starts before preflight and must fit the 60-second API objective. Pod replacement
time is recorded separately from endpoint availability.

This is a graceful single API Pod loss test. It does not prove abrupt process/host
failure, worker ownership during a run, PostgreSQL failover or inference resilience.
Those remain separate required procedures. Partial fault/probe records survive an
unsuccessful test. Both commands need the workspace dependencies and the pinned
Playwright browser installed; they read the existing password file in memory and
log out their own session. Never run the mutating regression suite against the
live namespace as a substitute for this scoped test.
Repeating a completed fault operation returns its original evidence without
reading credentials or injecting another failure. Its probe record is created
atomically and cannot overwrite another observation. An interrupted/incomplete
proof requires inspection; post-recovery probes cannot impersonate observation
of the original intervention.

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
The encoded verifier checks spread replicas and deployed digests, uses each API
Pod's actual connector to prove verified PostgreSQL TLS/non-superuser access,
and authenticates through the native HTTPS Service over a private SSM/API tunnel.
It preserves the intended hostname/SNI and verifies the internal CA without
changing system trust or using an insecure TLS mode. Cookies/CSRF values stay in
memory and the proof logs out its own session. A bounded synthetic file verifies
shared artifacts across all API/worker Pods, with exact-content cleanup.
The bundle records explicit exclusions: external frontend, failure recovery,
certificate rotation and GPU/inference acceptance still require separate tests.
Then test API/worker loss and PostgreSQL failover using the objectives in
`docs/architecture/platform-availability.md`. Record recovery times, Job UIDs,
execution timelines and partial artifacts. The external frontend, real Lambda
lifecycle, inference stack, telemetry and retained-state rebuild remain separate
required gates; this release procedure does not establish them.

## First live deployment evidence

The reviewed release completed on 2026-11-03 at 19:03:42 UTC. Live verification
`6a5d3214f09c` passed at 19:04:09 UTC: two Ready replicas per role, with each role
spread across two CPU hosts; both API connectors used TLS 1.3 as the non-superuser
management role; authenticated HTTPS returned the topology and all 21 retained run
records; all four API/worker Pods read a shared synthetic canary. Cleanup is
confirmed. These are deployment/connectivity results, not failure-recovery results.

The frontend release completed at 19:28:41 UTC with reviewed digest-addressed
images from source `e347fc0`. Check `965990a269cb` reconfirmed six spread Ready
application Pods, verified database TLS and shared artifacts. The three-zone ALB
then passed exact-target health and authenticated public-certificate HTTPS checks,
with its two-rule WAF attached. DNS was published by a saved one-alias-create plan.
The public browser test passed at 19:32:17 UTC, exposing the topology/inspector and
all 21 retained runs with Secure/HttpOnly cookies and zero browser errors.
All historical displayed results remain explicitly synthetic. No GPU or live
inference result is established by these checks.

API fault `3b562aa6e841` ran at 19:37:19 UTC. All 20 authenticated probes returned
HTTP 200; two spread Ready API replicas were observed again about 10.8 seconds
after the intervention. The selected Pod UID disappeared and a new Pod UID joined
the unchanged Deployment. A later browser read at 19:39:58 UTC still returned all
21 historical runs. Repeating the operation reused the original proof with an
unchanged checksum. This sampling result does not exclude sub-second outages or
establish worker/database/host-loss behavior.

`management-evidence` copies an explicit allowlist of plans, rendered values,
reference-only credential metadata, image scan receipts, public CA and verification
records. It verifies image receipt checksums and checks against the actual private
credential values before copying. No cookie, CSRF token, signing key or password
belongs in this bundle. Retain the generated manifest and its per-file SHA256
checksums outside Git. An external frontend/public DNS, certificate renewal,
application failover and remaining platform gates are still outstanding.
