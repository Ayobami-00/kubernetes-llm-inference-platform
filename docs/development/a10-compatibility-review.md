# Single-A10 compatibility review

This is planning evidence, not an allocated GPU or a tested serving release.

On 2026-11-03 the authenticated Lambda capacity API lists `gpu_1x_a10` at
129 cents/hour, with Virginia (`us-east-1`) and California capacity. The account
has no instances. Keep the first allocation in Virginia, one GPU at a time with
an explicit lease; do not rent it while its setup path is unfinished.

NVIDIA GPU Operator 26.7.2 is the current supported patch. Its official matrix
covers Ubuntu 24.04 and Kubernetes 1.36, but validates **containerd 2.0–2.3**.
The existing CPU bootstrap pins containerd 2.4.2. The GPU preparation path therefore
needs a separately pinned supported 2.3 patch; do not silently downgrade healthy
CPU hosts or claim the matrix validates 2.4.2. Mixed runtime versions across hosts
must be recorded in inventory and tested. Confirm the actual image/kernel before
installing operator-managed drivers/toolkit/device discovery/DCGM.

The official llm-d release API now identifies **v1.0.0** (2026-10-20), newer than
cached search results mentioning 0.10. Its release matrix lists endpoint picker
1.0.0, inference-extension CRDs 1.6.2, Gateway API CRDs 1.6.2 and Envoy Gateway
1.8.1 / Envoy AI Gateway 0.7.0. Its NVIDIA model-server table uses upstream vLLM
0.30.0, while the general summary lists 0.31.0 for some accelerators. Follow the
specific aggregated NVIDIA recipe and pin the exact tested images/settings;
these entries alone are not a proof of A10/Qwen compatibility.

The vLLM release API's `latest` points to a release candidate (`v0.33.0rc3`);
filtering stable version tags identifies 0.32.0 as the latest stable release.
Do not accidentally use a release candidate through the GitHub `latest` alias.
Prefer the compatible llm-d recipe over choosing the newest engine independently.

WriterAssist remains Qwen3-4B, BF16, one GPU, no speculative decoding, non-thinking
chat-template settings and its authored 128-token output cap. A10 advertised
24 GB is not proof of 24 GiB allocatable model memory; inspect actual NVML values
and validate a new serving-profile revision without rewriting the draft.

Sources checked on 2026-11-03:

- [GPU Operator platform matrix](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/26.7/platform-support.html)
- [GPU Operator release notes](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/release-notes.html)
- [llm-d 1.0.0 release](https://github.com/llm-d/llm-d/releases/tag/v1.0.0)
- [vLLM stable 0.32.0](https://github.com/vllm-project/vllm/releases/tag/v0.32.0)
- [Lambda Cloud API](https://docs.lambda.ai/api/cloud)
