# Management-driven live inference validation

## Gates and evidence

CPU acceptance uses A's actual AIPerf image through C, with fixture-labelled stub responses:

```sh
uv sync --locked --all-packages --all-extras
KLIP_WORKLOAD_INTEGRATION=1 ./dev/test-integration
```

This creates an isolated disposable kind cluster, verifies C's existing imports/recovery/browser/
backup procedures, then executes all five local profiles and both mixed scenarios through A's Jobs.
It checks worker replacement, stable Job UID, snapshot equality, report download and cancellation.
The final browser check imports a derived bundle and launches/downloads it from the UI. Evidence is
written under ignored `artifacts/`. Shared CPU hosts can exceed the strict scheduling-lag policy:
these runs retain invalid measurements and no SLO assessment. The harness checks that classification
and the strict CLI exit gate; it does not certify benchmark delivery capacity. The live acceptance
command below requires valid delivery. The single-host test volume does not prove production RWX storage.

The live gates are WriterAssist, remaining standalone profiles, llm-d mixed scenarios, then sustained
and failure runs. No GPU execution is implied by CPU acceptance. Keep all public samples, model
caches, credentials and execution artifacts outside Git.

## Required B handoff

Do not rent or provision resources from the runner. Before live validation B must supply actual GPU
capacity, its remaining lease, fresh full inventory, ready vLLM, production artifact storage and
appropriate observation access. The first deployment uses Qwen3-4B and direct vLLM. Public gateway
and Route 53 validation are separate from this internal baseline.

The handoff directory contains real, validated inputs:

- `gec-profile.yaml`: a new, non-draft serving revision (normally `gec-baseline@2`), immutable
  image digest/model/tokenizer commits, matching hardware and `chat_template_kwargs` with
  `enable_thinking: false`. Speculation remains disabled. B must verify the pinned image's supported
  chat-template configuration, model revision and effective generation settings.
- `latency-workers.yaml`: a new exact worker-group revision with verified SKU/region/hardware.
- `inventory.json`: current shared inventory with external GPU ownership, exact node UIDs and
  separate host/WireGuard/Cilium/GPU evidence.
- `versions.json`: `ExecutionVersions` with the exact runner image digest, AIPerf `0.13.0`, evaluator
  `0.1.0`, and actual semantic versions for `kubernetes`, `cilium`, `vllm`, `serving-chart`. For
  `llm-d-vllm`, use actual `llm-d` and `model-pool-chart` versions instead of `serving-chart`.
- `bindings.json`: an array of exact customer/profile/group references and deployment IDs.
- `external-capacity.json`: C's external-capacity import envelope, including inventory ID, the real
  lease, complete worker observations, deployment UID and endpoint.
- `groups.json`: mapping from exact worker spec references to C's external runtime group IDs.

Values above describe required inputs, not placeholder deployment pins. B's tested versions/digests
are authoritative. Align C's configured cluster API URL with the inventory's endpoint and supply the
required deployment labels/profile checksum described in the integration notes. Reserve enough CPU
for runner Jobs alongside management/control-plane services. Start with one CPU and 512 MiB memory
reserved for the runner, a two-CPU/two-GiB limit, and verify delivery diagnostics on that capacity.

Import the inventory and external capacity using the UI's reviewed imports or their durable API
endpoints. Wait for completed receipts. C must have a pinned runner image matching `versions.json`,
the execute-only command, appropriate mounted Secret references and shared RWX artifact storage.

## Prepare and freeze WriterAssist

Run preparation/planning inside the runner image with the same `/artifacts` path used by C. Set
`RUNNER_IMAGE` to B's tested digest, `HANDOFF_DIR` to the input directory, and `ARTIFACTS_HOST` to the
shared artifact directory mounted by management. These are operator-provided paths/image references.

```sh
docker run --rm -v "$HANDOFF_DIR:/handoff:ro" -v "$ARTIFACTS_HOST:/artifacts" "$RUNNER_IMAGE"   --config serving-profile=/handoff/gec-profile.yaml   --config worker-group=/handoff/latency-workers.yaml   prepare --customer writer-assist --source public --limit 1000   --serving-profile gec-baseline@2 --output /artifacts/prepared

docker run --rm -v "$HANDOFF_DIR:/handoff:ro" -v "$ARTIFACTS_HOST:/artifacts" "$RUNNER_IMAGE"   --config serving-profile=/handoff/gec-profile.yaml   --config worker-group=/handoff/latency-workers.yaml   plan --scenario writer-assist-baseline --source public --prepared /artifacts/prepared   --mode benchmark --serving-stack direct-vllm --endpoint "$INFERENCE_ENDPOINT"   --bindings /handoff/bindings.json --inventory /handoff/inventory.json   --versions /handoff/versions.json --output /artifacts/plans/writer-live
```

Use exact references from the handoff if their revisions differ. Add `--credential-env
writer-assist=INFERENCE_TOKEN` when the endpoint needs authentication; C's configured execute command
must map that variable to an allowlisted Secret file. Never insert token values in commands or URLs.

Preparation renders the same non-thinking template used in serving and computes real token lengths
and artifact checksums. JFLEG development records are warmup-only; test records are evaluation-only.
Canonical WriterAssist needs more than the earlier 100-record development sample. Preparation may
select up to the actual available corpus; exact schedule exhaustion fails before launch. Do not
silently reuse inputs or substitute a smaller tokenizer.

Refresh observations before planning and launch within C's readiness freshness window. A stale
bundle requires a new frozen plan and identity, not an edit to the previous snapshot. Verify endpoint
health/model alias with an unscored connectivity request before launching the canonical scenario.
A verifies `/v1/models` for direct vLLM and makes a bounded, unscored one-token completion probe for
each customer alias. The llm-d path uses routed probes without requiring a gateway model catalogue.
Probe metadata is retained separately from measured records; fixture-identifying responses fail.

## Launch, collect and assess

Use the UI to select the imported bundle, exact runtime groups and Check readiness, then Launch real
run. The equivalent operator command imports its bundle, launches idempotently and collects registered
artifacts:

```sh
uv run --locked --all-packages workload-runner submit   --bundle "$ARTIFACTS_HOST/plans/writer-live/management-bundle.json"   --management-url "$MANAGEMENT_URL" --password-file "$ADMIN_PASSWORD_FILE"   --groups "$HANDOFF_DIR/groups.json" --operation-key writer-live-baseline-001   --output artifacts/live-validation/writer-baseline-001 --require-valid
```

Reusing the operation key observes the original attempt. Use a new key and evidence directory for a
new run. Ctrl-C requests cancellation and collects the terminal state. Client observation timeout or
network loss never creates a replacement attempt; inspect the saved launch identity through C.

Keep the authored 60-second warmup, 180/60/360 scored phases, 120-second drain, rates, one-second
completion target, 95% attainment, 300-second windows and 100-request minimum. Completion with a
violating SLO is a valid measured outcome. Accept this gate only with valid delivery and reconciled
measurements, genuine JFLEG GLEU and retained deployment/preparation evidence. Report the observed
latency and quality rather than interpreting an experimental target as a capacity promise.

Collected evidence includes the management run, checksummed normalized/raw artifacts, quality,
report and timeline origin. `evaluate` on a retained attempt writes a new versioned assessment;
it does not rewrite the original result. Keep the original execution directory on durable storage.

## Remaining profiles and mixed scenarios

Repeat preparation, explicit pinned planning and management submission for TutorChat, DocFlow,
ResearchAgent, then CodeAgent. Review each profile's real tokenizer/context limits and selected
sample count. CodeAgent preserves adapted recorded contexts/tools and does not execute tools or
measure functional agent success. TutorChat's pedagogical quality remains not evaluated.

DocFlow and ResearchAgent bind to the same medium deployment ID. Introduce B's pinned llm-d release
before validating `shared-contention` and `platform-mixed`; select `--serving-stack llm-d-vllm` and
record its actual routing/chart versions. Verify all required latency/shared/large capacity and
leases before the full-platform run. Keep endpoint/network path and client location explicit when
comparing an internal direct baseline with public gateway/DNS results.

## Sustained and controlled failure campaigns

Add `--sustained` to planning to derive 20 scored minutes: steady 180 s, burst 60 s, recovery 960 s.
Warmup, drain, rates and SLOs remain unchanged. Each variant has a distinct identity and recorded
lineage. Prepare enough unique inputs for its materialized schedule. JFLEG's test split cannot
support an hour at the authored rate without reuse; do not represent separate runs as one continuous
hour. Inputs may be reused between separately recorded runs.

First complete clean sustained WriterAssist and platform-mixed runs. Then freeze a separate campaign
manifest alongside a new mixed plan/evidence directory, listing intended interventions, exact target
namespaces, deployment UIDs, categories and measurement-relative offsets. Fault actuation is an
explicit operator/B action, disabled in ordinary runner execution. Do not put kube-admin credentials
inside runner Pods.

Compute measurement start as C's `timeline_origin_at` plus the frozen warmup. In the separate fault
run, restart one management API Pod at scored minute 4, one management worker Pod at minute 8, and one
serving Pod at minute 12. Check the deployment UID and selected Pod ownership before each action;
wait for replacement readiness before the next intervention. Record intended/actual UTC times,
Pod/deployment UIDs, the original runner Job UID and recovery evidence in `interventions.jsonl`.
Retain that journal and campaign-manifest checksum with the collected assessment. Preserve affected
SLO windows and failures; do not discard them to obtain a passing aggregate.

The API/worker replacements must preserve the same runner Job UID and engine timeline. Serving
restart may legitimately cause rejected/failed requests and violating windows; generator delivery
must still be assessed independently. Separately terminate a runner Pod and verify terminal failure,
partial artifacts and no automatic restart. Test node loss only after B supplies redundancy across
distinct GPU nodes. Control-plane/etcd fault experiments remain B's workstream.

## Current live gate

Local integration does not supply GPU leases, production RWX storage or a tested vLLM image/model
handoff. Until B supplies those inputs, keep live, mixed and sustained/failure acceptance pending.
Never replace the missing evidence with documentation addresses, fixture flags or invented pins.
