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
exact workload subjects and repository permissions. Host roles, control-plane
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

Host installation and actual positive/negative image pulls still require live
verification before management deployment. Disable metadata fallback in the
provider, match only the project registry paths, require the role annotation and
use token-scoped credential caching. Keep ordinary CI independent of live cluster
resources and this opt-in source build.

Sources: [Kubernetes 1.36 credential providers](https://v1-36.docs.kubernetes.io/docs/tasks/administer-cluster/kubelet-credential-provider/),
[audience restriction](https://v1-36.docs.kubernetes.io/docs/reference/access-authn-authz/node/#service-account-token-audience-restriction),
[pinned helper source](https://github.com/kubernetes/cloud-provider-aws/tree/v1.36.1/cmd/ecr-credential-provider),
and [ECR pricing](https://aws.amazon.com/ecr/pricing/).
