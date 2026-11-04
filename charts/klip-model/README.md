# Aggregated model release

This is the initial single-GPU model chart for the A10 integration path. It is
render-tested, not evidence of a live model deployment. The surrounding static
llm-d/Envoy releases, authenticated gateway, certificate/DNS setup, serving
mutation adapter and live acceptance remain separate requirements.

Supply values derived from a pinned `ServingProfile`, its exact document SHA,
the deployment identity and a fresh executor-verified Node UID. Empty defaults
intentionally cannot render an executable release. Never substitute a guessed
Node UID, model/image revision or hardware observation.

The initial schema permits one GPU and one replica. The Pod selects the protected
GPU-ready, worker-class and Node-UID labels; it cannot tolerate bootstrap
quarantine. It uses the operator-managed NVIDIA RuntimeClass, no API token or
host mounts, an unprivileged user, a read-only root and bounded writable caches.
Its one-GPU `Recreate` strategy avoids an unplanned surge allocation and does
not provide serving HA. Startup includes bounded model download/compilation.

Non-thinking template defaults, prefix-cache disablement, natural EOS behavior
and the customer output ceiling are explicit. In vLLM 0.30, the
`max_new_tokens` generation override supplies an upper bound through
`get_max_tokens`; a smaller client-requested limit is preserved. Requests with
controls outside the frozen customer contract must still be rejected by the
gateway/admission policy. Speculation and remote model-code execution are absent.
Actual image CLI compatibility and effective settings require live verification.

Weights/cache are ephemeral. A replacement Pod may download the pinned model
again; include that startup time in recovery evidence. `/dev/shm` counts against
the Pod memory limit. Do not assume these initial cache/resources/shm settings
are suitable for the later eight-GPU model; that expansion requires its own
validated profile and chart update.

The Service is private. NetworkPolicy permits only the specifically labelled
gateway, endpoint picker and Prometheus Pods on port 8000; runner Pods cannot
bypass the gateway. Those trusted components share the model HTTP port, so this
is network identity isolation, not HTTP-path authorization. Egress permits DNS
and public HTTPS for the fixed public model, excludes private/metadata ranges,
and provides no cloud identity or private credentials.

Validate with a resolved values file before the management-owned Helm release:

```sh
artifacts/tools/bin/helm lint charts/klip-model --values <resolved-values.yaml>
artifacts/tools/bin/helm template <deployment-id> charts/klip-model \
  --namespace llm-d --values <resolved-values.yaml>
```

Retain chart/version/hash, effective values, image/model/tokenizer digests,
native Deployment/Pod UIDs and the full GPU/serving readiness evidence with each
run. A successful render or Ready Kubernetes Node cannot replace those gates.

Pinned upstream behavior inspected:
[vLLM 0.30 output-limit implementation](https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/entrypoints/serve/utils/api_utils.py) and
[chat serving](https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/entrypoints/openai/chat_completion/serving.py).
