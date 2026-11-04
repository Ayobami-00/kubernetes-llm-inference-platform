# Encrypted topology-aware PostgreSQL storage

The static EBS CSI release is pinned to driver 1.67.2/chart 2.67.2. Its images,
including optional sidecars, use verified Linux AMD64 manifest digests from the
platform lock. It is installed once with two controller replicas on separate CPU
hosts and one Linux node plugin per protected platform worker.

```sh
make storage-access-plan
make storage-access-apply PLAN_SHA=<reviewed-plan-digest>
make cluster-platform-protect
make platform-services-fetch
make platform-service-deploy SERVICE=aws-ebs-csi-driver
make cluster-verify-ebs
```

The access-layer Terraform update creates the exact controller identity and policy;
it does not create volumes. IAM uses this cluster's retained OIDC issuer and exact
`kube-system:ebs-csi-controller-sa` subject. It permits encrypted gp3 creation up to
200 GiB with both CSI and cluster tags. Existing volume operations require both tags;
attachment targets are the three current platform instance ARNs. Creation-time
tagging cannot relabel a control-plane disk into this scope. Snapshot/copy/fast-restore
and arbitrary existing-resource tagging privileges are not granted. The instances'
SSM roles and management credentials are unchanged. Default AWS-managed EBS encryption
was exercised; custom KMS keys require a separately scoped policy/recovery review.

The controller has explicit region configuration and projected-token credentials.
The upstream optional static-key Secret fallback is removed. Node plugins receive
no AWS role and use `metadataSources=kubernetes`, with SDK IMDS fallback disabled.
Authenticated AWS observations supply instance-type and region labels after checking
instance IDs, type, AZ, project/node tags and running state. Provider IDs and topology
labels remain checked against the recorded node UIDs. The conservative initial limit
is ten CSI attachments per worker; it does not represent a hardware maximum.

`klip-gp3` specifies encrypted ext4 gp3, 3000 IOPS/125 MiB/s, Retain, expansion and
WaitForFirstConsumer. It is not the default class. Allowed topology is the three
project AZs. Each PostgreSQL instance uses independent data/WAL claims. An EBS volume
stays in its AZ; cross-AZ PostgreSQL availability comes from replication, not moving
one disk across AZs. Host-loss recovery must account for replacement capacity in the
volume's AZ. Retain is not a backup and does not protect against namespace deletion
followed by an explicitly authorized volume purge.

## Live canary and cleanup boundary

The verifier allocates exactly three 1-GiB synthetic claims in its own UUID namespace.
It verifies non-root writes, disappearance of old VolumeAttachments, fresh consumer
reads, actual AWS encryption/type/size/AZ/tags and expected attachment identities.
After deleting its owned namespace, it checks that every PV is Released and every
volume still exists with no attachments. Only then does it switch these exact synthetic PVs to
Delete using UID, volume-ID, claim-UID and prior-policy JSON Patch preconditions.
The CSI controller performs deletion; both PV disappearance and AWS volume deletion
must be confirmed. Normal application claims remain Retain.

The report, Jobs and synthetic data markers are private operator evidence under
`cluster/ebs-probes/<operation>`. A failed/incomplete operation blocks another canary
allocation. Inspect and recover its recorded namespace/PV/volume identities before
retrying; do not delete by a broad project-tag filter or bypass the guard.

The initial management database requests 180 GiB (3 × 40 GiB data + 20 GiB WAL), about
$16.70/month at the recorded London gp3 rate, excluding transfer/backup/API costs.
A temporary three-instance restore adds another 180 GiB while retained; remove only
its verified test claims/volumes after recovery acceptance. The canary's full-month
3-GiB equivalent is about $0.28, with deletion confirmed after the test.

Sources: [pinned installation/metadata requirements](https://github.com/kubernetes-sigs/aws-ebs-csi-driver/blob/v1.67.2/docs/install.md),
[driver options](https://github.com/kubernetes-sigs/aws-ebs-csi-driver/blob/v1.67.2/docs/options.md),
[upstream V2 policy](https://github.com/kubernetes-sigs/aws-ebs-csi-driver/blob/v1.67.2/docs/AmazonEBSCSIDriverPolicyV2.json),
and [EC2 IAM conditions](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ec2.html).
