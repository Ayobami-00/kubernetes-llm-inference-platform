# Opt-in A10 console acceptance

These commands use the repository's installed Playwright runtime against the
actual public HTTPS management console. They are not ordinary browser tests and
must never run automatically in offline CI. No route interception, provider mock
or simulated allocation is used.

Install the management web development dependencies and its Playwright Chromium
browser first. The script reads the existing `dev/.secrets/admin_password`
directly; no password or session token is printed, passed in command arguments,
saved in browser traces, or retained in the evidence bundle.

```sh
# Read-only provider/configuration inspection; Refresh creates a durable read job.
node scripts/infrastructure/a10_ui_acceptance.mjs \
  inspect readonly artifacts/acceptance/<new-inspection-directory>

# Publish the verified revision through the UI, preserving latency-workers@1.
node scripts/infrastructure/a10_ui_acceptance.mjs \
  publish-profile readonly artifacts/acceptance/<new-profile-directory>

# Explicit paid action: at most one Virginia A10, $1.29/hour, 90 initial minutes.
# Optional final argument watches approved stock for at most 900 seconds.
node scripts/infrastructure/a10_ui_acceptance.mjs \
  provision writer-a10-<unique-suffix> artifacts/acceptance/<new-launch-directory> 900
```

Each evidence directory must be new. Configuration publication accepts either
the original revision without an existing draft, or the already-published exact
revision 2. Unknown edits are not overwritten. The exact published document is
versioned at `deploy/environments/lab/worker-groups/latency-workers-v2.yaml`.
Its hardware remains the advertised description; later measured usable memory
has a separate observation.

The stock watch uses the UI's durable Refresh operation and requires a newly
observed provider result. It does not turn an old available-stock card into
launch permission. It refreshes at bounded intervals, keeps type/region/price
fixed and never substitutes another offering. An unavailable result submits
no worker create. The UI's own enabled-state guard must also pass.

Before clicking Provision, the script writes `provision-intent.json`. It retains
the actual create response and operation/resource IDs, then captures the real
operation view. A response loss or terminal browser error must be reconciled
through management's retained operation; never remove an intent to repeat a
submission. Management's durable intent, ownership and provider ledger remain
authoritative. A UI 202 response proves admission, not provider allocation,
successful joining, GPU readiness or inference.

Artifacts include credential-free screenshots, the exact published profile,
provider observations and launch receipts. Browser errors print only stage and
exception class because Playwright errors can include field-fill arguments.
The local browser context is closed at completion and session storage is not
exported. Retain failed checks alongside successful checks.

Once admitted, monitor the existing operation and worker member through management.
Record its provider ID, native host-task Job UIDs, lease deadline, actual kernel,
GPU/CUDA/network proof and fresh DCGM observations. Confirm termination through
the same management workflow when the experiment ends. No serving/model/SLO
gate is complete merely because this browser script succeeds.

## First paid bootstrap timeout, 4 November 2026

The real UI admitted `writer-a10-20261104-b` as operation
`op-9f64299963344461`. Lambda allocated Virginia A10
`d42f3dc7064b45158ea3075e3f86f1ce` at 10:18:28 UTC. The VM became active,
but no authenticated host-key callback arrived. Management expired the bootstrap
at 10:38:30, initiated its unbootstrapped cleanup path, confirmed provider
termination at 10:40:22 and cleared the cleanup obligation at 10:40:27.
No host task, cluster join, GPU readiness or inference result was claimed.
The allocation-to-confirmation estimate is approximately $0.47 at $1.29/hour;
this is not a provider invoice.

The retained evidence is in the ignored acceptance directory
`artifacts/acceptance/a10-ui-replacement-20261104/`. In particular, its terminal
operation and member observations retain the timeout and confirmed termination.
Do not repeat this operation or reuse its expired credential.

Bootstrap V3 starts Ubuntu's native SSH service before the explicit `sshd -t`
check. This permits systemd to establish SSH's runtime directory on socket-based
early boots. Exact effective source restrictions and disabled password/root
authentication are still checked before attesting. The early-boot runtime-directory
race is a corrected failure mode, not a proven diagnosis of the terminated VM.
V1/V2 launch programs and their existing hashes remain reproducible.

If V3 reaches a bootstrap exception, it can report only a fixed stage code over
the same authenticated, expiring HTTPS nonce. It sends no exception text, host
private key or command output. A failure never establishes host trust, cannot
replace an accepted key and triggers the existing cleanup workflow. If cloud-init
or outbound connectivity fails before that report is possible, the bounded
attestation deadline still applies. An absent callback never authorizes blind
SSH host-key acceptance.

The next operation, `op-178da799f9bb4c56` / `writer-a10-20261104-c`,
successfully attested its host key at 10:57:13 UTC. Its native preparation Job
failed before changing that host: the image's public containerd archive was
owned by root with mode `0600`, unreadable by UID 20002. The image now verifies
its installed artifacts again after switching to that unprivileged UID, and
newly staged public runtime archives use `0644`.

Cleanup confirmed the provider fence but initially could not complete mesh
reconciliation because the CPU handoff intentionally omits operator-local
firewall file paths. Mesh now requires a scoped firewall file for new workers;
only the six named AWS CPU peers can preserve an existing regular, root-owned
`0600` firewall. It does not replace those retained CPU firewalls.

When this exact never-joined failure prevents an image repair, prepare the normal
image-only executor upgrade with `--fenced-recovery-task <failed-revocation-id>`.
The helper binds all task/observation/receipt bytes, native Job UIDs and the
original provider fence. Its native guard repeats those checks while holding
both writer locks, requires exactly the six CPU peers and proves the GPU Node
is absent. Running tasks, another allocation's pending cleanup, changed evidence,
joined Nodes and unfenced recovery remain forbidden. It neither clears the
reservation nor rewrites failed history. After repair, submit a new management
cleanup operation and confirm its completed revocation before a fresh rental.

The repaired executor completed that cleanup as `op-3c6b6f5ce7ad45b0` at
11:27:17 UTC. The next attempt, `writer-a10-20261104-d`, reached native
Ansible gathering facts but SSH timed out. Its automatic cleanup completed at
11:39:56, including provider termination and address release. No host kernel,
driver or GPU facts were observed by that failed preparation.

The platform worker AWS security group allowed package downloads and WireGuard,
but omitted the executor's outbound TCP 22 connection to Lambda. The reviewed
Terraform plan `22c82c1ee1e933d6daffb9cc37bf6beb8b5bf4a829bcf352642c04075998fdba`
added only `packages_and_mesh["lambda_ssh"]`, rule `sgr-0542142e783eb6a79`.
It changed no instances, volumes or ingress rules. Pod egress remains restricted
by the host Job NetworkPolicy; Lambda's SSH source restrictions and strict host
key verification remain required. Both platform-compute mock tests passed.
This removes a confirmed network prerequisite; a new native preparation must
still establish SSH, OS compatibility and GPU readiness.
