# Availability and recovery policy

These are initial acceptance objectives for the experiment environment. They are
not measured results or customer guarantees. Record actual timings, lost work and
exceptions in each verification bundle before treating a gate as satisfied.

| Component / event | Intended behavior | Initial acceptance objective |
| --- | --- | --- |
| One management web or API Pod lost | Another host serves requests; an existing runner keeps its identity and timeline | Healthy endpoint within 60 seconds; no duplicate launch or resource allocation |
| One management worker lost | Durable ownership expires and another worker reconciles the same operation / Job | Reconciliation resumes within 120 seconds; same runner Job UID |
| PostgreSQL primary lost | CloudNativePG promotes an eligible synchronous standby; clients reconnect with verified TLS | Write access returns within 120 seconds; zero acknowledged transactions lost for a tested single-node fault |
| One persistent CPU worker lost | Remaining hosts retain application and database quorum | Management and database recover within 180 seconds without using control-plane capacity for applications |
| Two PostgreSQL hosts unavailable | Preserve the selected synchronous durability policy | Writes may stop; do not silently weaken replication to obtain availability |
| Runner Pod or process lost | Fail the attempt and retain existing artifacts | Terminal failure observed within 120 seconds of confirmed loss; no new execution clock or automatic rerun |
| Single A10 serving Pod / host lost | Report inference errors and affected SLO windows honestly | No serving HA claim; measure restart / replacement recovery separately |
| Restore from external PostgreSQL archive | Restore into a distinct archive identity; validate histories and operations before reopening mutations | Recovery point within 5 minutes when archiving was healthy; initial recovery-time objective 60 minutes |
| Retained-state environment rebuild | Rebuild versioned infrastructure, restore data and identities, then repeat WriterAssist | Record elapsed time and recovery point; initial recovery-time objective 4 hours, excluding unavailable GPU capacity |

The timers start at the recorded intervention or confirmed loss, as appropriate.
Measure endpoint recovery with authenticated application requests and database
recovery with committed reads/writes. A Running Pod, successful restart or health
probe alone does not establish recovered application behavior. A second planned
intervention waits until the preceding fault has recovered.

## Failure domains and durability

The three persistent platform workers occupy distinct London availability zones.
The management roles use at least two replicas, required host separation and
disruption budgets. Planned rollouts replace one replica without a surge. A
disruption budget constrains voluntary eviction; it does not prevent host loss.
CPU/memory reservations and the frozen runner placement protect the control plane.

PostgreSQL has three instances on independent data and WAL volumes, one required
synchronous standby and failover quorum. An acknowledged transaction is expected
on the primary and a standby before success is returned. This trades write
availability for durability when enough eligible standbys are unavailable. Live
failover tests must verify the acknowledgment boundary; the settings alone are
not evidence of zero loss.

Zonal EBS volumes remain in their original AZ. Fail over to another PostgreSQL
replica rather than assuming a volume can attach across AZs. External base backups
and continuous WAL archives cover loss beyond replication. Backup age, archive
lag, failed archive operations and restore verification require alerts. The
five-minute recovery-point objective depends on healthy archiving and must be
measured by restoring a known committed marker.

The pinned Barman plugin manager currently runs as one recoverable replica. Its
CNPG-I gRPC service is leader-gated while the upstream readiness probe only checks
the manager's health endpoint; increasing its replica count would advertise an
inactive endpoint. WAL sidecars run with every database instance. Record manager
replacement time and archive/reconcile behavior during its failure test. Do not
describe this manager as HA or infer backup availability from database readiness.

Management stores operation/attempt ownership durably. Idempotency prevents a
restart from allocating another VM or creating another runner attempt. Ambiguous
provider submissions require reconciliation, not a blind retry. Runner Pods use
one execution attempt; they do not restart an interrupted benchmark.

Durable artifacts retain the frozen inputs, schedule, request records, partial
results, final results and reports. High-frequency benchmark I/O uses local
scratch where possible, with explicit durable checkpoints. Until checkpoint-loss
tests establish a bound, report the actual last durable record; do not claim
zero artifact loss or fabricate missing measurements.

## Retained-state rebuild boundary

The destructive acceptance environment is the tagged project `lab` environment in
the configured AWS account/region plus its explicitly recorded Lambda instances.
Retain Terraform bootstrap/state, encryption and signing keys, recovery material,
external backups and experiment history. Verify an independent recovery copy and
a real restore before teardown. Do not destroy unrelated resources or use a purge
path. Require exact instance and Kubernetes UIDs during removal.

The rebuild evidence must show fresh host/cluster identities where expected,
restored management history, working credentials and certificate trust, a newly
UI-provisioned A10, telemetry recovery, and a new real WriterAssist result. Restoring
only the database or merely rendering the infrastructure does not pass this gate.

## Evidence

For each fault, save the source revision and deployment pins, intervention time,
affected identities, probes, ownership transitions, database markers, request
windows, observed recovery time and data/artifact loss. Keep performance verdict,
measurement validity and execution state separate. Record capacity unavailability
explicitly rather than substituting a different GPU or claiming a successful test.
