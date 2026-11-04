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
