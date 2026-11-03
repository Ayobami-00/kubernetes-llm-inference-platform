# Rolling platform CPU maintenance

The full telemetry and one-runner forecast requires approximately 9.86 CPU and
22.19 GiB of declared requests. The original three 4-vCPU/16-GiB workers expose
only seven allocatable CPUs after one worker is lost. The versioned envelope now
selects three `m7i.2xlarge` workers, with at least 7.5 CPU/29 GiB allocatable each.
The forecast must be reconciled with final releases and measured failure behavior.
It is not a performance or recovery proof.

London Linux on-demand compute at the 2026-11-03 price review increases from
$510.49 to $1,020.98/month at 730 hours. Root/data disks and IPv4 are unchanged;
other services, transfer and tax are additional. `c7i.2xlarge` was also compared:
the selected M7i tier provides twice its memory for about $92/month more across
three workers. The account's 32-vCPU Standard quota covers the final 30-vCPU total,
including the three existing control planes. The apply rechecks current quota.

Fresh rebuilds use the new default size. Existing clusters must use the serialized
maintenance procedure below. A normal Terraform apply rejects a multi-node resize
and rejects a resize without a matching fresh drain receipt. Per-node overrides
preserve the other two sizes during each step, and outputs report actual per-node
types instead of pretending a mixed rollout is homogeneous.

## Preconditions and maintenance

Ensure the three protected worker identities, all database instances and the
management replicas are healthy. Verify a completed external base backup and WAL
archive proof using the existing recovery workflow. Do not launch experiments
during maintenance; the operator checks for active runner/migration Jobs before
draining and immediately before applying. Confirm the selected instance/AZ and
the incremental resource estimate before each saved plan is applied.

Prepare and review one standby-host plan first:

```sh
make platform-resize-prepare NODE=platform-01 BACKUP_OPERATION=<verified-backup-operation>
make platform-resize-apply PLAN_SHA=<exact-reviewed-plan-sha256>
make platform-resize-observe
```

Wait for terminal `completed` before preparing another host. Initially use the
standby hosts first, then the current primary host after observing its placement.
Never assume the primary is on a fixed node. Each plan can update only one exact
existing instance's size and reviewed CPU options; root disk, ENI/EIP, image,
identity, tags and security settings must remain unchanged. Deletions, replacements
and unrelated updates are rejected.

The operator checks Node UID/resourceVersion before cordoning and respects drain
deadlines/PDBs without forced eviction. CloudNativePG coordinates drain and primary
switchover. Two Ready database replicas, the original synchronous quorum and two
Ready replicas of each management role must be available off the drained host
before EC2 changes. Do not weaken quorum to make this pass.

The selected ALB target is temporarily deregistered and its connection drain must
finish while both other exact targets remain healthy. The target is registered
again only after worker/database recovery, and completion requires three healthy
targets. Terraform retains durable attachment ownership; the maintenance journal
records this temporary membership change and its compensation. An interrupted
pre-apply drain can restore the original target through the abort procedure.

CPU options are explicit: four cores and two threads per core for `m7i.2xlarge`.
EC2 can otherwise preserve the old reduced CPU settings across a type change.
The pinned Terraform provider may perform another stop/start to update CPU options;
the worker remains drained throughout. The deployer receives this one additional
action only for the three project/cluster/environment-tagged worker slots. Node
roles and the management backend receive no new privilege.

Scheduling reopens only after AWS reports the intended size and the original
Kubernetes Node is Ready with eight real CPUs and sufficient allocatable CPU/memory.
The instance-type label is updated with UID preconditions. Completion requires
all three workers/database instances healthy and the original identities/storage
intact. Preserve each private plan, journal and result. A repeated terminal apply
returns the original result; an interrupted AWS apply is observed without replaying
Terraform automatically.

## Interrupted maintenance

Use `platform-resize-observe` and inspect the original Terraform process/remote
state before taking action. An observation timeout does not authorize another
apply. For `applying` or `recovering`, repeating the same approved resize command
only observes recovery and never submits another Terraform update.

If a drain failed before any Terraform apply, explicitly restore scheduling:

```sh
uv run --group infrastructure python scripts/infrastructure/platform_resize.py abort-drain --approve-plan <original-sha>
```

This is allowed only in `prepared` or `draining`, after confirming the exact EC2 instance is
still running at the original type. It retains the old truthful label and records
a failed attempt and restores a temporarily drained ALB target. It cannot undo an ambiguous AWS transition. Never force-delete
database PVCs, force promotion, or silently downgrade a partially changed worker.

After all three resizes, recheck authenticated management/history, database/WAL,
artifact access, CSI and the full telemetry capacity envelope. Planned drained
maintenance does not replace the separate unplanned CPU-worker-loss acceptance.

References: [EC2 CPU options](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-cpu-options-rules.html),
[CloudNativePG node maintenance](https://cloudnative-pg.io/docs/1.28/kubernetes_upgrade/).
