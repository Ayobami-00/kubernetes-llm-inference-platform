# Private project images

The retained `registry` Terraform root owns three private London ECR repositories:
`klip/management-backend`, `klip/management-web` and `klip/workload-runner`. It
retains tagged image pins for historical runs and rebuilds, refuses forced
repository deletion, uses AES256 encryption and immutable tags, and requests
basic scan-on-push. Its lifecycle policy expires only untagged images after 30
days. Do not silently replace a historical tag or delete a digest referenced by
an execution binding.

The current registry was applied from a reviewed create-only plan. Live read-back
checks its project tags, encryption, immutability, lifecycle and absence of extra
repository-sharing policies. Pull and publisher IAM roles are separate and have
exact workload/CI subjects and repository permissions. Host roles, control-plane
permissions and management's infrastructure permissions were not broadened.
This does not yet establish working private pulls, published images, scan results
or deployed management availability.

```sh
make infra-registry-plan
make infra-registry-apply PLAN_SHA=<reviewed-saved-plan-digest>
make registry-verify
```

The root is administrator-owned like retained storage/identity. The normal
foundation and inference DNS identities cannot deploy it. Keep this state and its
repositories through retained-state rebuild; it is not an application teardown
target. The registry has no fixed compute charge. At the reviewed $0.10/GB-month
rate, 5 GB is about $0.50/month before transfer, optional features and taxes.
Same-region transfer to the existing EC2 workers is free under the published ECR
pricing. Recheck costs if images are pulled across regions/providers or scanning
mode changes. The account was BASIC at initial review; this root does not own
account-wide scanning configuration.

## Kubelet credential helper

The planned pull path exchanges a Pod-bound service-account token for the exact
read-only ECR role. It does not place AWS credentials in the application container
or store a registry password Secret. Kubernetes 1.36.5 enables the necessary
service-account credential-provider feature by default. Namespace-scoped audience
RBAC allows only the six declared image-pull accounts to request the STS audience
through NodeRestriction; it does not grant arbitrary token audiences.

Use the distinct `eks.amazonaws.com/ecr-role-arn` annotation understood by the
pinned helper. The AWS Pod webhook's `eks.amazonaws.com/role-arn` annotation serves
a different purpose and must not be substituted. The management chart gives web
and migrations explicit accounts. The migration account is a pre-install/upgrade
hook created before its Job. Static audience authorization must already exist.

The upstream 1.36.1 binary endpoint was unavailable at verification, so the helper
is built from the unmodified upstream release commit. The source archive, Go
builder image and resulting Linux AMD64 executable are pinned in
`deploy/ecr-credential-provider.lock.json`. Its build Job runs the upstream tests,
checks Go modules, compiles with a fixed toolchain/flags, and exports the binary
through checksum-verified bounded ranges. Local disk headroom was insufficient
for a large Docker build; this bounded Job uses existing CPU capacity instead.

```sh
# Explicitly identify a new build; a repeated operation reconciles its existing Job.
make image-tools-build OPERATION=<12-lowercase-hex-characters>
```

The build requests 500m CPU / 512 MiB, limits usage to 2 CPUs / 2 GiB and 8 GiB
scratch, runs non-root without a service-account token or cloud credentials, and
does not touch control-plane hosts. It creates no VM or GPU. The verified output
is cached outside Git. A changed or unreviewed executable cannot be installed.
The build Job and its immutable source ConfigMap are removed with UID-conditional
cleanup. A transfer retry reads the same compiled bytes; it never recompiles or
creates a replacement attempt. The first live build passed upstream tests and
produced a 14,315,646-byte executable with confirmed cleanup.

Host installation has passed live verification on all three CPU workers. Actual
positive/negative image pulls still require verification before management deployment.
Disable metadata fallback in the
provider, match only the project registry paths, require the role annotation and
use token-scoped credential caching. Keep ordinary CI independent of live cluster
resources and this opt-in source build.

The encoded rolling installation checks all three protected node UIDs, fresh node
leases and the healthy three-instance database before every host. It refuses
unknown kubelet units/drop-ins, disabled Pod-token support, links and writable
configuration. It installs the verified binary, exact three-repository match and
token-scoped cache, with metadata and shared credential-file fallback disabled.
The unit appends a dedicated argument variable and preserves kubeadm flags,
resource reservations and user arguments. Ansible keeps private rollback copies,
checks the local health endpoint and restores the previous files on installation
failure. A later cluster-level failure stops further hosts and retains evidence.
Control-plane kubelets are excluded. A failed rollout requires explicit inspection
and a fresh preparation; it never silently continues on another host.

```sh
make image-credentials-prepare
make image-credentials-apply PLAN_SHA=<reviewed-host-plan-digest>
make platform-service-deploy RELEASE=klip-image-pull
```

Review the complete private `cluster/image-credentials/plan.json` before applying.
Host plans bind source, binary checksum, original configurations and exact node
identities; changed inputs require a new plan. The separate pull-audience release
must exist before pulling private images. Installation/readiness alone does not
prove authentication: run actual positive and wrong-identity pulls with
`imagePullPolicy: Always`, including on hosts that have cached the image.

The first rolling application completed on 2026-11-03 at 18:27:55 UTC. All three
unchanged node identities had the frozen executable checksum and configuration;
kubelet configuration, kubeadm flags and user argument files matched their
pre-install hashes. Database/node checks passed between updates. No control-plane
kubelet was changed. This evidence is separate from actual private-pull acceptance.

Actual pull acceptance subsequently passed at 18:34:16 UTC (operation
`308dd332f9bc`). The patched backend image ran on all three hosts using the explicit
probe account. A missing annotation and the same account name in a different
namespace were denied. All probes used `Always`, including when image bytes were
already cached. They had no listener, credentials, volumes or service-account
mount. All five Jobs/Pods and the temporary namespace were removed through
UID-conditional deletion; cleanup is confirmed. An initial probe-harness race
between completed Pod and delayed Job status was corrected with a regression
test; the earlier test objects were also cleaned up.

```sh
uv run --locked --group infra python scripts/infrastructure/image_pull_acceptance.py \
  --image <verified-backend-repository@sha256:digest>
```

The verifier records mutation intents and UIDs before advancing, reconciles an
ambiguous create by reading its exact ownership annotation, and never interprets
network failure as identity denial. It retains failures and incomplete cleanup.
This proves the tested pull path; it does not deploy an application or prove its HA.

## CI publication

The first private-branch CI identity audit verified GitHub's signature, issuer,
audience, expiry, repository ID and ref without retaining the signed token. Its
subject includes immutable owner/repository IDs. Configure CI publisher trust from
that verified subject; do not assume the older name-only subject format. The
publisher role now trusts only the verified immutable-ID subject on
`codex/end-to-end-platform`, recorded in `infra/terraform/registry.json`. Its
former unused Kubernetes publisher subject was removed. Main and pull-request
subjects have no publishing access. Adding a release branch requires an explicit
Terraform trust review; merely changing a workflow cannot broaden IAM trust.

The reviewed follow-up plan created the retained GitHub provider and updated only
the publisher trust and repository-scoped metadata/scan reads. It changed no
compute, repository, host role or pull permissions. AWS manages GitHub's signing
endpoint certificate trust; no fabricated certificate thumbprint is configured.
If a clean account already has this provider, explicitly adopt the verified
resource into state before planning; never create a second owner or replace it.

`platform-images.yaml` uses commit-pinned Actions and short-lived OIDC credentials
to build Linux AMD64 backend, web, runner and stub images on hosted CI. Stub and
runner share one repository with distinct component-prefixed immutable tags.
Re-running the same source commit reuses its existing digest. Access denial is
not interpreted as an absent image. No application source or image is made public.
Production backend/web base images are also digest-pinned.

Each component retains a `klip-private-image.v1` receipt containing source commit,
Dockerfile/lockfile checksums, workflow identity, digest reference and complete ECR
BASIC findings. The single-platform image manifest enables that scanner. CI keeps
build records separately; it does not claim an attached signed provenance or
SBOM. A complete scan is evidence, not security approval: review findings and the
combined checks before choosing deployment pins. Download receipts into retained
operator evidence as well as the 90-day CI artifact store. An unfinished or
unsupported scan fails the publishing check. Images are never automatically
deployed by the publishing workflow.

The first CI publication (`9567a39`) succeeded for all four components. Its scans
found critical base-package issues, so those images were held back from application
deployment: backend 6, web 26, runner 8 and stub 8 critical findings. The follow-up
recipes pin maintained Python 3.12.16 / Nginx 1.30.6 images; rebuilding, scanning and
the combined execution/browser checks must verify those replacements before they
are selected for deployment. Historical tags/findings remain available for audit.
See the vendor [OpenSSL](https://security-tracker.debian.org/tracker/CVE-2026-75803),
[Perl](https://security-tracker.debian.org/tracker/CVE-2026-42496) and
[Nginx](https://nginx.org/en/security_advisories.html) advisories. A version upgrade
alone does not establish that every finding has been resolved.

The Python 3.12.16 backend rescan removed all six critical findings, with eight
high findings still awaiting vendor/applicability review. Nginx 1.30.6's full Alpine
variant still included vulnerable curl/brotli packages; the next recipe uses the
upstream digest-pinned `alpine-slim` variant without those optional runtime
packages/modules. Its scan and browser/runtime checks remain required. The
publication workflow watches actual application, contract and configuration
inputs as well as Dockerfiles, so source changes cannot silently leave an old
image as the proposed deployment.

Clean-checkout CI also exercises the OCI chart fetch. Helm may choose a filename
different from the lock's cache name when pulling by digest. The fetcher downloads
into an isolated directory, verifies the original checksum and then places the
archive at the locked path. A fresh real upstream download and positive/tampered
archive regression tests verify this behavior without weakening the checksum lock.

Sources: [Kubernetes 1.36 credential providers](https://v1-36.docs.kubernetes.io/docs/tasks/administer-cluster/kubelet-credential-provider/),
[audience restriction](https://v1-36.docs.kubernetes.io/docs/reference/access-authn-authz/node/#service-account-token-audience-restriction),
[pinned helper source](https://github.com/kubernetes/cloud-provider-aws/tree/v1.36.1/cmd/ecr-credential-provider),
and [ECR pricing](https://aws.amazon.com/ecr/pricing/).
