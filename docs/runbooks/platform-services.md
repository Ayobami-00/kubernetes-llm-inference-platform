# Certificate, identity and artifact storage add-ons

These releases are installed once per cluster through supported upstream components.
No custom provider/GPU lifecycle operator is introduced. The operator CLI checks
protected CPU placement, verifies exact chart checksums and uses immutable image
digests. It holds the same operation lock as the host/cluster tools and uses the
existing private, CA-verified SSH-over-SSM API tunnel. It cannot install arbitrary
release names. A Helm success is recorded separately from live acceptance evidence.

```sh
make platform-services-fetch
make platform-service-deploy SERVICE=cert-manager
make platform-service-deploy SERVICE=klip-workload-identity
make platform-service-deploy SERVICE=aws-efs-csi-driver
make platform-service-deploy SERVICE=klip-storage
make identity-verify-injection
make cluster-verify-storage
```

The fetch step only downloads checksum-locked charts. Existing archives with a bad
checksum are rejected. Each extraction starts empty, preventing obsolete/injected
cache templates from surviving. Installs use Helm atomic wait, bounded timeouts and
release history. Review CRD changes separately on version upgrades; retained CRDs
are not rolled back by an ordinary Helm rollback.

## Versions and compatibility

The reviewed candidates are cert-manager 1.21.3 (upstream tests Kubernetes through
1.36), AWS pod identity webhook 0.6.19 and EFS CSI 3.5.4/chart 4.5.4. Current upstream
release metadata was inspected on 3 November 2026; the nine image manifest digests
were resolved and checked for Linux AMD64. They are recorded with chart checksums
in `deploy/platform-services.lock.json`. Driver/runtime acceptance remains a live
check, not an inference from the version numbers.

cert-manager has two replicas each of controller, webhook and CA injector, with
hard hostname separation, AZ spreading, disruption budgets and explicit resources.
The token webhook has the same HA placement. Its external cert-manager certificate
is watched and reloaded by the upstream binary; the removed CSR bootstrap path is
not used. Certificate renewal and failure behavior still require live testing.

## Scoped projected-token injection

`charts/klip-workload-identity` packages the upstream AWS webhook binary, not a new
credential controller. Its Kubernetes privileges are read-only ServiceAccount
metadata. It has no IAM credentials, Secrets API permissions or CSR privileges.
It injects short-lived STS-audience projected tokens based on the account annotation.
The existing retained HTTPS issuer and exact IAM trust policies remain required.

Fail-closed admission matches only the explicit namespace/account pairs for EFS,
EBS, the two PostgreSQL identities and the isolated identity canary. Other system
and application Pods do not depend on this webhook. CEL includes a missing-field
guard. The injector itself does not match its admission condition. This scope must
be updated deliberately when introducing another AWS-enabled workload.

Annotations do not grant IAM privileges: each role still checks the exact OIDC
provider, namespace, ServiceAccount and audience. Verify actual mutated Pod volumes,
environment and real SDK calls. An annotation or healthy webhook alone is not a
credential-renewal test. SDK/STS secrets and token contents must never be retained.

## Static EFS ownership

Terraform owns the regional filesystem and single access point. The EFS chart's
controller is disabled; no dynamic filesystem/access-point provisioning is used.
The pinned upstream chart renders controller RBAC even with that setting, so the
bounded Helm post-renderer removes that unused account/bindings/roles and rejects
unexpected resources. Only the five reviewed node-driver resources are retained.
The node account has read-only node metadata; its mutation permission is disabled.

The driver is a privileged host-network component only on protected CPU workers.
It uses the scoped artifact role, disables SDK IMDS fallback and resolves EFS using
the host DNS path so the local AZ mount target is selected. This does not grant the
management/runner Pods AWS credentials. Static PVs must specify the retained access
point and `tls,iam`, with reclaim policy Retain. Creating the driver alone does not
prove mounting, RWX consistency, retained data or recovery.

Sources: [cert-manager compatibility](https://cert-manager.io/docs/releases/),
[official OCI installation](https://cert-manager.io/docs/installation/helm/),
[upstream token webhook](https://github.com/aws/amazon-eks-pod-identity-webhook/tree/v0.6.19),
[upstream EFS release](https://github.com/kubernetes-sigs/aws-efs-csi-driver/releases/tag/v3.5.4),
and [CSI IAM/mount semantics](https://github.com/kubernetes-sigs/aws-efs-csi-driver/blob/v3.5.4/docs/parameters.md).


The static claim `klip/klip-artifacts` uses Retain and the access-point handle, with
TLS/IAM mount options and protected CPU affinity. Its 100Gi advertised Kubernetes
capacity is not an EFS quota or a capacity reservation. The operator verifies driver
readiness before binding it. `cluster-verify-storage` writes small synthetic files as
UID/GID 10001 on all three workers, checks all nine cross-worker reads from new Jobs,
then removes exactly those canary files. It never deletes the claim or real artifacts.
Failed checks preserve partial evidence/data. Job cleanup uses API-side UID preconditions;
name-reuse conflicts are errors, not permission to delete a replacement resource.
