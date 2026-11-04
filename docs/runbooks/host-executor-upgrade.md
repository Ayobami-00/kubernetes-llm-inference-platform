The executor has a separate, image-only upgrade path. It preserves the original
custody handoff, peer registry, encrypted EFS access point, SSH keys, TLS Secrets,
native task receipts and initial release record. It appends upgrade evidence to
`active.json`; it never re-runs initial installation or copies old Mac peer state
over the authoritative executor volume.

The current procedure requires only the six persistent CPU peers and no
unfinished or cleanup-pending host task. Run it before the first GPU admission.
Extending this procedure to an active GPU fleet needs a separate compatibility
and availability review.

1. Commit and publish the executor source. `Platform experiment images` builds
   its Linux AMD64 image, verifies its offline dependency closure, retains the
   ECR image receipt, and captures a full Trivy scan of OS/Python packages and Go
   binaries on the isolated CI worker. CI uses its verified publishing role;
   operator-local AWS credentials and scanner caches are unnecessary.
2. Download that commit's `private-image-host-executor-*` and
   `host-executor-security-*` artifacts outside Git. Complete the exact image,
   source and High-finding review in `deploy/host-executor-risk-review.json`.
   Critical findings, stale evidence, or unreviewed changed High findings prevent
   deployment. A scan or successful build alone is not runtime approval.
3. Prepare and inspect the bounded plan:

   ```sh
   .venv/bin/python scripts/infrastructure/host_executor_upgrade.py prepare \
     --receipt <image-receipt.json> --security-directory <scan-directory>
   ```

   The plan binds the source tree, image assessment, existing Deployment/PVC and
   credential UIDs, custody fingerprints, previous release, values and rendered
   manifests. Its delta must change only the executor container image and the
   matching `HOST_EXECUTOR_IMAGE` binding for subsequent native Jobs. It cannot
   change resources, storage, access, network policies or secret mounts.
4. Apply the saved plan using its printed hash:

   ```sh
   .venv/bin/python scripts/infrastructure/host_executor_upgrade.py apply \
     --approve-plan <approval-sha256>
   ```

   A single non-retrying native Job acquires the actual shared `dispatch.lock`
   and `cluster/operator.lock` before preflight. Busy ownership fails rather
   than stealing a lock. New task submissions are held while the guard remains
   active. The new image verifies workload identity and all six strict CPU SSH
   paths, without changing CPU hosts. Helm rolls the two API replicas with its
   existing availability policy and automatic rollback. Promotion of the local
   active receipt requires both new runtime image digests to be Ready on
   distinct CPU hosts, unchanged credential/storage identities and the same
   active guard Job UID. The guard has a native 900-second maximum lifetime.
5. Re-run the mutual-TLS/RBAC boundary acceptance with a fresh evidence identity:

   ```sh
   .venv/bin/python scripts/infrastructure/host_executor_acceptance.py \
     --identity <twelve-hex-identity>
   ```

All plans, previous values/manifests, original active state, native Job/Pod UIDs,
preflight results, Helm output and final evidence remain under the private
`cluster/host-executor/upgrades/<approval-sha256>/` directory. Secret contents
are neither retrieved by the identity check nor retained in these reports.
The guard runs preflight in its own native process and freezes a redacted result
on the executor volume before signalling readiness. Exec calls only read saved
receipts. Failed checks retain exception types and code locations,
without exception messages, subprocess output or credentials.

An existing attempt is never automatically restarted. After a lost response or
failed apply, inspect its `result.json`, bound native Job UID, Helm history and
actual API Pod images before reconciling. `reconciliation_required` does not
claim that a rollback succeeded. A guard-release transport failure is recorded;
the native deadline releases the locks. Do not remove retained receipts, create
a replacement Job under the same attempt, or reconstruct custody from old local
files. Preserve evidence before preparing a separately identified recovery.

This runbook describes implementation and acceptance requirements. A deployment
is verified only by the retained live release and boundary evidence.

Verified on 2026-11-04 at 07:51:58 UTC: source image `2f377fa`, digest
`sha256:1b8934ea32c61b599bcec6604e78f334ae1e08ae41ab06a066f879b7ba192356`.
Upgrade `3199d27cedbc5318e94b9a7fd9ee6669b96db6ed9d3ef6940434af2fa4d4355c`
retained the original handoff and credential/storage UIDs. Both actual image
digests became Ready on `platform-02` and `platform-03`. Native preflight
verified all six strict CPU SSH paths and registry revision 12. Boundary probe
`b9c06f43d812` verified mutual TLS, both certificate rejection cases, typed task
rejection, native API identity and RBAC denials. This establishes the CPU
executor boundary; GPU admission remains separately unconfigured.

The evidence retains two terminal pre-deployment failures and their reconciled,
unchanged original releases. After moving preflight under native Job ownership,
Helm completed successfully. An immediate observation preceded both new Pods
becoming Ready; the same release was subsequently reconciled against its exact
live manifest without reapplying Helm. The implementation now performs a bounded
post-Helm observation for both new digests, while refusing custody identity drift.
