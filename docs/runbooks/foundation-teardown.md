# Foundation teardown

Teardown deletes all three CPU instances, their root disks, **all three standalone
etcd disks**, ENIs/EIPs, NLB and VPC resources. It preserves the state bucket,
bootstrap IAM roles/policies, domain registration and public hosted zone. This is
the agreed full-foundation behavior; individual instance replacement preserves
the separate etcd disks.

1. Identify cluster `klip` and inspect current inventory/account. Withdraw any
   inference origins through the reviewed DNS workflow before decommissioning.
   Coordinate workload shutdown; the wrapper is not a Kubernetes drain tool.
2. For a real cluster, take an etcd snapshot, verify it with the version-compatible
   etcd tooling (including restore verification as appropriate), copy it outside
   these resources and record its hash. `--confirm-backup-verified` is the operator's
   attestation to that verification; checking a file/hash alone cannot prove a
   restorable etcd snapshot.
3. If Kubernetes has never been bootstrapped and the disks contain no cluster data,
   use the explicit empty-data acknowledgement instead. The wrapper additionally
   checks over SSM for Kubernetes/etcd artifacts, a listener on6443 and disk
   signatures/partitions. It refuses ambiguous disks; do not format them to make
   the check pass.

```bash
make infra-destroy-plan DESTROY_ARGS='--confirm-cluster klip --ack-empty-etcd'
# Or, after actual backup verification:
make infra-destroy-plan DESTROY_ARGS='--confirm-cluster klip --backup-file /absolute/off-cluster/snapshot.db --backup-sha256 <hash> --confirm-backup-verified'
```

The first plan disables instance termination protection and NLB deletion protection
only. It refuses unrelated changes. Review it and apply with
`make infra-destroy PLAN_SHA=<reviewed-hash>`. Then repeat the same destroy-plan
command to generate the actual destroy plan; review every deletion and apply that
new exact hash. Evidence is rechecked before each application. There is no automatic
destroy following the protection update.

Verify that instances, six disks, EIPs, load balancer and VPC resources are gone,
that S3 state remains readable/versioned, and that the retained registrar/zone/IAM
objects still exist. Retain reports, the backup and state version IDs according to
your recovery needs. Removing the private SSH key or retained bootstrap infrastructure
is a separate explicitly authorized action. Do not delete state to conceal orphaned
resources or bypass protection.
