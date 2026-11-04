# Retained A10: WriterAssist n-gram experiment

The current scope is only WriterAssist/Qwen3-4B on the user-retained A10.
Do not rent other capacity, terminate/reboot/replace this VM, attach an automatic
lease, or adopt it into provider-managed worker-group cleanup. External ownership
continues after any experiment or onboarding failure. The user owns shutdown.

The operator procedure is separate from the fresh Ubuntu 24.04/GPU Operator
driver-installation path. This Lambda Stack Ubuntu 22.04 host already has an
NVIDIA driver and toolkit. Preserve them. A dedicated `klip-containerd` service
uses the existing binaries with its own socket/root/state; Lambda's Docker and
containerd services remain untouched. Preparation verifies the same boot ID and
driver before and after. It contains no reboot, runtime restart or provider API.

Establish the SSH pin through the instance's provider-authenticated HTTPS
Jupyter connection before preparation. Keep the public inspection, independent
known-hosts pin, runtime inspection and ownership record in an ignored evidence
directory. Never store Jupyter tokens or provider credentials in these records.

```sh
PYTHONPATH=scripts/infrastructure .venv/bin/python scripts/infrastructure/retained_gpu.py \
  prepare --evidence artifacts/acceptance/manual-a10-20261104
PYTHONPATH=scripts/infrastructure .venv/bin/python scripts/infrastructure/retained_gpu.py \
  operator-preinstalled --evidence artifacts/acceptance/manual-a10-20261104
PYTHONPATH=scripts/infrastructure .venv/bin/python scripts/infrastructure/retained_gpu.py \
  join --evidence artifacts/acceptance/manual-a10-20261104
```

The operator overlay disables GPU Operator driver/toolkit installation. It must
remain active while this preinstalled host is attached. The device plugin, GPU
feature discovery, validators and DCGM remain operator components. Joining alone
does not establish GPU readiness or remove bootstrap quarantine.

The join Job holds the authoritative executor's dispatch and peer-registry locks,
uses its current pinned image/credentials, and runs once with `backoffLimit: 0`
and `restartPolicy: Never`. Neither local stale peer registries nor management
provider lifecycle operations participate. It revokes its short-lived join token
and retains failures. An explicit repair uses `--previous-failed-result` with the
exact checksum of the prior failed pre-join result, records a separate execution,
and preserves the failed Job/result; this is never an automatic retry.

Compare two distinct frozen serving profiles and runs: baseline without
speculation, then `engine.speculativeDecoding` with `method: ngram`,
`num_speculative_tokens: 5`, `prompt_lookup_min: 2`, `prompt_lookup_max: 5`.
The chart serializes these into vLLM's `--speculative-config`; other speculative
methods and reversed lookup bounds fail. Keep Qwen3-4B/model/tokenizer/image pins,
non-thinking chat template, output cap, prefix-cache setting, source selections,
seed, rates, timeout and measurement policy identical between comparisons.

Use the existing public JFLEG preparation with development-only warmup and test
evaluation, all four references, genuine GLEU, immutable schedules and reports.
Run a short separately identified connectivity/delivery check before canonical
scenarios. Record startup/compile effects separately from steady measurements.
Report client completion/TTFT, valid delivery, eligible request/window counts,
SLO attainment, output length, GLEU/coverage and available speculation counters.
Do not invent counters or interpret absent acceptance data as zero. N-gram
benefit depends on repeated token spans; improvement is an experimental outcome.

The existing one-second completion target remains experimental. No single-A10
serving HA or production readiness claim follows from a successful experiment.
Clean only experiment-owned releases/test resources when necessary; leave the VM
and its external ownership protection in place.

Versioned upstream references:
[vLLM 0.32 speculative configuration](https://github.com/vllm-project/vllm/blob/v0.32.0/vllm/config/speculative.py),
[NVIDIA GPU Operator 26.7 preinstalled stack](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/26.7/getting-started.html).
