# etcd recovery and retained backups

The backup bucket has its own Terraform state and remains after foundation teardown.
Three daily timers run at02:00,02:15 and02:30UTC, with up to60seconds jitter. Each
produces a live etcd snapshot, PKI, Secret-encryption configuration and integrity
metadata, then encrypts the bundle with age before S3 upload. The recovery objective
is24hours; failed or overdue backups fail `cluster.py backup-check` and remain in
journals. No external alert destination has been configured in this milestone.

The shared CPU IAM role can PutObject only under the credential's own
`clusters/klip/${aws:userid}/` prefix (role unique ID plus EC2 instance ID). It cannot read/delete backups.
The operator reads exact S3 versions. C receives no backup/AWS credentials. IMDSv2,
hop1, a Cilium workload metadata-deny policy and a forwarding guard reduce workload access; trusted privileged
host workloads still share the host's trust boundary.

Current objects expire after30days; noncurrent versions also expire after30days.
S3 lifecycle actions are asynchronous, and versioning can retain bytes beyond the
current-object window. Include those bytes in storage costs. The bucket uses SSE-S3
in addition to client encryption. No recurring KMS key is required.

The age private identity stays outside the cluster, bucket, Terraform and Git.
`cluster.py key-verify-copy --copy PATH --confirm-independent-custody` verifies the retained copy without exposing
its contents. Keep that copy in removable/offsite storage and preserve access to it
through operator machine loss. Without that identity, encrypted backups cannot be
recovered. Copy verification records a path/hash, not the key.

`cluster.py backup-check` downloads exact recorded object versions, verifies their
ciphertext hash, decrypts privately and checks snapshot contents/identity/integrity.
With the separate probe registered, it additionally restores into an isolated etcd
container using the cluster's actual etcd image and a revision bump/compaction mark.
The test uses loopback ports12379/12380 and never joins the production etcd cluster.
It copies only the snapshot to the probe; the age identity and private PKI remain
with the operator. Plaintext test files and containers must be removed afterward.

That test demonstrates snapshot restore and registry reads, not complete recovery
of all control-plane hosts. Full disaster recovery requires a separate reviewed
maintenance procedure: stop all old members, verify one selected snapshot/CA and
Secret-encryption key, restore a new coherent member set, reconfigure static Pods,
then verify API writes, controllers, node certificates, workloads and inventories.
Do not mix restored and old live members. For one failed member, preserve quorum
and use explicit member replacement rather than restoring the entire cluster.

Before foundation deletion, download and verify a recovery bundle, retain it and
the age identity independently, and pass its verified file/hash to the existing
teardown workflow. All three etcd disks are deleted with foundation teardown.
The backup bucket, state bucket, bootstrap IAM, domain and hosted zone remain.
