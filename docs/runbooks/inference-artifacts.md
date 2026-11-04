# Pinned inference artifacts

`deploy/inference.lock.json` records the base aggregated serving candidates:
llm-d router 1.0.0, vLLM 0.30.0, Envoy Gateway 1.9.3, AI Gateway 1.2.0,
Gateway API 1.6.2 and Inference Extension 1.6.2. AI Gateway's upstream project
is now named Agent Router; its chart/image/API names remain unchanged.
The newer gateway pair replaces the older llm-d guide's gateway example and
still requires our own integration tests. No advanced serving optimization is
enabled by these pins.

Fetch and verify without installing anything:

```sh
PYTHONPATH=scripts/infrastructure .venv/bin/python \
  scripts/infrastructure/inference_artifacts.py fetch
```

The fetcher verifies all five chart archives and four source/CRD artifacts,
including existing cached files. Only the v1 InferencePool CRD is selected;
the experimental InferencePoolImport CRD is not needed. Helm receives an empty
registry configuration rather than local AWS/GitHub/Docker credentials.
Raw downloads remain under ignored `artifacts/platform/inference/`.

The image resolver reads only anonymous public registry metadata. It verifies
manifest/configuration bytes, architecture, descriptor sizes and digests,
selects an unambiguous Linux AMD64 child and does not download container layers.
Anonymous pull tokens are neither returned nor retained, and registry tokens
are removed before permitted blob-CDN redirects.

The opt-in **Inference image security** workflow scans all six exact manifests
on disposable CPU runners and retains complete native findings. It neither
approves deployment nor changes the GPU Operator review. A completed scan does
not establish compatibility, payload protection, kernel security, serving
readiness or measured inference behavior. Critical runtime findings remain a
deployment blocker; High findings require exact applicability review.

The model Helm chart is separate from gateway/controller foundations. Dynamic
model-release ownership belongs to management; shared CRDs/controllers belong
to static platform deployment. Do not give both owners the same release.
