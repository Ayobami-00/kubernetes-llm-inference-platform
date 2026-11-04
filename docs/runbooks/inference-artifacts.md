# Pinned inference artifacts

`deploy/inference.lock.json` records the base aggregated serving candidates:
llm-d router 1.0.0, vLLM 0.32.0, Envoy Gateway 1.9.3, AI Gateway 1.2.0,
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

The first vLLM 0.30.0 scan failed with 31 Critical tuples: 22 in kernel headers
and nine in Python dependencies. It is retained as a superseded, unapproved
candidate. Revision 2 selects upstream's newer stable 0.32.0 for a separate scan;
that selection is not a finding waiver or a readiness claim. Both model candidate
documents remain draft and retain the same Qwen/tokenizer pins.

The upstream 0.32.0 scan retained 18 unique Critical tuples: 15 kernel-header
findings and three CA/tooling findings. `dev/model-serving/` derives an image
from that exact AMD64 digest and updates only certifi, pip (including its
vendored certifi) and uv, using checksum-locked PyPI wheels. It compares the
complete Python distribution inventory before and after; CUDA, vLLM, Torch,
Transformers and Tokenizers remain unchanged. The build also runs `pip check`
and repeats verification as UID 10001. Runtime Python/Requests/curl CA settings
point to the patched bundle. No compiler/header removal is attempted because
upstream uses runtime JIT compilation.

The **Pinned model serving image** workflow builds on a disposable CPU runner,
publishes an immutable source tag, extracts native patch/header inventories
without starting serving, and retains a complete Trivy scan. Any Critical
findings still fail its scan gate; neither the patch nor its inventories waive
findings. Exact applicability review and hardware readiness remain separate.

Terraform's retained `klip/model-serving` repository is private, encrypted,
immutable and scans pushes. Tagged historical images survive rebuilds; only
untagged images expire after 30 days. The applied plan was
`825112c2124e13fb66de2b4f442e6c972a4fe1ed199452752c0f68a893a92af5`.
It added the repository/retention and the exact
`system:serviceaccount:llm-d:klip-model-release` read-only role, and extended
the existing repository-bound CI publisher to this one repository. Existing
application pull identities retain their original three-repository scope.
The new role can read model and release-helper images, cannot publish, and
does not give model Pods Kubernetes credentials. One approximately 9 GB image
costs roughly $1/month in registry storage, excluding transfer and later images.
