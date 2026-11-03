# Self-managed Kubernetes workload identity

The retained `identity` Terraform root owns `https://oidc.klip-lab.click`, its IAM
OIDC provider and two public signing-metadata documents. A private, encrypted,
versioned S3 bucket is served through CloudFront origin access control. Its bucket
policy permits this distribution to read only the discovery document and JWKS.
It contains no service-account private key, application data or credentials.

This is an explicit self-managed Kubernetes federation foundation. EKS automatic
identity integration is not assumed. Workload access requires subsequent exact
namespace/service-account/audience role bindings, token projection and verification.
Publishing an issuer alone does not grant AWS access or complete the storage gate.

## Ownership and retained assets

Use the existing administrative bootstrap profile for this rare retained
identity/IAM foundation. The operator enforces the configured account and exact
source administrator. It does not expand the foundation/DNS deployment roles and
does not provide administrator credentials to management, runner or GPU Pods.
Runtime controllers use separately scoped roles.

State lives under `workload-identity/terraform.tfstate` in the retained state bucket.
The root owns only the `oidc` A/AAAA records and its ACM validation CNAME in the
existing zone. Inference DNS remains owned by its separate root. Do not introduce
another controller for these records.

The S3 origin remains in London. CloudFront's viewer certificate uses ACM in
`us-east-1`, as required by CloudFront. This certificate-region exception does not
change the cluster or GPU region. The footprint adds no compute and serves two
small JSON objects; account pricing, requests, object versions and transfer determine
its metered cost. Record the concrete resource plan and estimate before applying.

The bucket, issuer, CloudFront distribution, certificate and issuer DNS have
destruction protection. Keep them through the retained-state environment rebuild.
Restore the original service-account signing material from the verified, independently
retained Kubernetes recovery bundle. Recreating CPU hosts does not authorize signer
replacement. Key retirement and a complete purge require separate reviewed procedures.

## Collect, plan and publish

```sh
make identity-collect
make identity-plan
# Review the exact saved plan, account, resources and source digest.
make identity-apply PLAN_SHA=<reviewed-identity-plan-digest>
make identity-verify-public
make identity-activate
```

Collection reads only `/etc/kubernetes/pki/sa.pub` through the already trusted host
transport. All three control planes must agree, and their key must match the live
Kubernetes JWKS. The private `cluster/public-signing-input.json` records source host
identities, public-key hashes and capture time. No private key is exported.
Planning rejects stale captures. Application rechecks the live signers against the
frozen variables before any mutation. Published signing-key replacement is refused;
rotation must preserve an explicitly reviewed overlap period.

The issuer publishes `.well-known/openid-configuration` and `openid/v1/jwks`, using
HTTPS and a maximum 60-second metadata cache lifetime. Verification checks both
public documents, their identities and the IAM audience. The public verification
receipt is private `cluster/workload-identity-public-verification.json`.

The committed Terraform test key is a generated public fixture, unrelated to any
cluster. It is never an input to live planning. The initial signer profile accepts
RSA 2048-8192, RS256 and the standard public exponent, validates each key ID against
its public encoding, and rejects private parameters.

## Kubernetes issuer transition and storage integration

`identity-activate` holds the operator lock and runs a bounded, rolling two-phase
transition. It verifies public metadata first, keeps the existing signing keys and
API audiences, and checks all three etcd members and API servers between changes.
It changes one static API manifest at a time, using hidden atomic temporary files
and private recovery copies. A failed update requests restoration of that host's
preceding manifest. Subsequent execution rechecks the actual state and does not
move an already-public primary back to the legacy issuer.

The transition tests an actual legacy TokenRequest token across the phases when a
legacy-primary server exists, verifies newly issued public-issuer tokens on every
API server, and checks that an STS-audience token cannot authenticate to the API
audience. Tokens and TokenReview request/response bodies remain in memory and are
not retained in reports, logs or command arguments. Temporary canary resources are
removed. It updates kubeadm's bootstrap ConfigMap, while the same final settings are
encoded in the repository's kubeadm generator. The private transition report records
each phase, key IDs and whether a legacy canary was available; it does not claim an
old-token check that could not be performed on an already-migrated cluster.

The complete integration sequence is:

1. Verify the existing signer and current API issuer/audience configuration.
2. Stage acceptance of both legacy and public issuers on every API server before
   changing the primary signing issuer. Preserve existing API audiences and signing
   keys. Update one API server at a time and verify health/quorum between steps.
3. Make the public issuer primary, retain legacy-token validation during migration,
   and encode the final flags in kubeadm bootstrap configuration.
4. Configure the supported token projection/SDK integration and narrowly scoped
   controller/backup roles. Do not grant application permissions to host roles or
   infer them from EKS annotations without the corresponding integration.
5. Verify real STS federation and negative namespace/service-account/audience cases,
   then install CSI/storage and PostgreSQL backups with those bindings.

Keep the metadata isolation policy. AWS-bound projected tokens use the STS audience;
they must not replace the normal Kubernetes API token. Credential renewal must use
short-lived workload identity rather than copied temporary access keys in Secrets.

References: [AWS self-hosted federation setup](https://github.com/aws/amazon-eks-pod-identity-webhook/blob/master/SELF_HOSTED_SETUP.md),
[Kubernetes issuer flags](https://kubernetes.io/docs/reference/command-line-tools-reference/kube-apiserver/),
[IAM OIDC provider](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc.html),
[CloudFront certificate region](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cnames-and-https-requirements.html).
