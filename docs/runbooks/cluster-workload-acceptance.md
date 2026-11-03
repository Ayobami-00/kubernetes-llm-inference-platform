# CPU workload acceptance on the deployed cluster

This procedure uses the actual pinned runner image, management Job ownership,
shared EFS artifacts and public authenticated management API. All inputs, token
accounting and inference responses remain explicitly synthetic. It is separate
from real inference/SLO calibration, and leaves the existing customer/scenario
revisions unchanged.

Start with the existing healthy management release, its fresh reviewed image
receipts, three healthy database instances and the protected CPU workers. Use one
operator and one runner at a time; these checks do not provide general production
admission control for competing users. No GPU or cloud API mutations are needed.

```sh
uv run --locked --all-packages --all-extras python \
  scripts/infrastructure/workload_acceptance.py stage --operation <12-hex-identity>
uv run --locked --all-packages --all-extras python \
  scripts/infrastructure/workload_acceptance.py run --operation <same-identity> \
  --scenario writer-assist-baseline
```

`stage` installs `charts/klip-workload-stub` privately using the reviewed stub
digest. The stub has no public Service, API token or egress. Only same-namespace
managed runner/preparation Pods may connect. It reuses the existing token-free
runner ServiceAccount for kubelet-mediated private ECR pulls; it receives no AWS
credentials. This fixture endpoint is deliberately a singleton.

A UID-bound, non-retrying preparation Job writes a separate
`/artifacts/acceptance/<identity>` tree, prepares all five deterministic corpora
and freezes all seven smoke bundles. Its writable mounts are the artifact claim
and bounded temporary storage; its root filesystem is read-only. Readiness,
resource budgeting, Pod admission and immutable image inputs are checked before
staging. Lost Jobs and ambiguous Helm operations are retained for diagnosis and
never blindly restarted. Failed preparation requires a new identity.

Run the remaining standalone IDs, then `shared-contention` and `platform-mixed`,
sequentially with the same operation. Imports and launches use stable idempotency
keys. The local receipt and management artifacts retain the actual run/attempt,
frozen snapshot, normalized records, raw exports, readable report and checksums.
A repeated command observes the original management attempt, including terminal
attempts. Run state and measurement validity remain separate; an invalid delivery
or violating SLO must never be hidden by retries or changed targets.

Inspect the imported run and report in the management UI. Cancellation,
management-worker recovery during a run and runner-Pod loss need separate recorded
tests. Remove only this owned fixture release when its tests are complete; retain
historical artifacts. This procedure does not establish GPU readiness, gateway
authentication, sustained performance or production readiness.
