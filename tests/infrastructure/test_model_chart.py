"""The initial model release is pinned, identity-bound and isolated from clients."""

import json
import subprocess

import pytest
import yaml
from common import ROOT, tool
from jsonschema import Draft7Validator


def values():
    value = yaml.safe_load((ROOT / "charts/klip-model/values.yaml").read_text())
    profile = yaml.safe_load(
        (ROOT / "deploy/environments/lab/candidates/gec-a10-integration.yaml").read_text()
    )
    value.update(
        profileRef="gec-a10-integration@2",
        profileSHA256="a" * 64,
        deploymentId="writer-assist-a10",
        nodeUID="11111111-2222-3333-4444-555555555555",
    )
    value["model"].update(
        repository=profile["model"]["repository"],
        revision=profile["model"]["revision"],
        tokenizerRevision=profile["model"]["tokenizer_revision"],
        alias=profile["model"]["alias"],
    )
    value["engine"]["image"] = profile["engine"]["image"]
    return value


@pytest.mark.parametrize("bad", ["image", "revision", "nodeUID", "extra", "thinking"])
def test_model_chart_rejects_unpinned_or_unsupported_inputs(bad):
    value = values()
    if bad == "image":
        value["engine"]["image"] = "vllm/vllm-openai:latest"
    elif bad == "revision":
        value["model"]["revision"] = "main"
    elif bad == "thinking":
        value["engine"]["chatTemplateKwargs"]["enable_thinking"] = True
    else:
        value[bad] = "unsupported"
    schema = json.loads((ROOT / "charts/klip-model/values.schema.json").read_text())
    assert list(Draft7Validator(schema).iter_errors(value))


@pytest.mark.skipif(
    not (ROOT / "artifacts/tools/bin/helm").is_file(),
    reason="Pinned Helm render runs in the infrastructure job after platform-tools installation",
)
def test_initial_model_chart_renders_exact_runtime_and_network_boundaries():
    result = subprocess.run(
        [
            tool("helm"),
            "template",
            "test-model",
            str(ROOT / "charts/klip-model"),
            "-n",
            "llm-d",
            "--values",
            "-",
        ],
        input=yaml.safe_dump(values()),
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    docs = {v["kind"]: v for v in yaml.safe_load_all(result.stdout) if v}
    pod = docs["Deployment"]["spec"]["template"]["spec"]
    assert pod["runtimeClassName"] == "nvidia" and not pod["automountServiceAccountToken"]
    assert pod["nodeSelector"]["klip.dev.node-restriction.kubernetes.io/gpu-ready"] == "true"
    assert (
        pod["nodeSelector"]["klip.dev.node-restriction.kubernetes.io/gpu-node-uid"]
        == values()["nodeUID"]
    )
    assert len(pod["tolerations"]) == 1 and pod["tolerations"][0]["key"] == "nvidia.com/gpu"
    assert pod["securityContext"]["runAsNonRoot"]
    container = pod["containers"][0]
    args = container["args"]
    assert json.loads(args[args.index("--override-generation-config") + 1]) == {
        "max_new_tokens": 128
    }
    assert json.loads(args[args.index("--default-chat-template-kwargs") + 1]) == {
        "enable_thinking": False
    }
    assert "--no-enable-prefix-caching" in args and "--disable-log-requests" in args
    assert not any("speculat" in str(a) or "trust-remote-code" in str(a) for a in args)
    assert container["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert container["securityContext"]["readOnlyRootFilesystem"]
    assert all("hostPath" not in volume for volume in pod["volumes"])
    assert docs["Service"]["spec"]["type"] == "ClusterIP"
    ingress = docs["NetworkPolicy"]["spec"]["ingress"][0]["from"]
    assert len(ingress) == 3 and all(
        "namespaceSelector" in peer and "podSelector" in peer for peer in ingress
    )
    assert all("runner" not in str(peer) for peer in ingress)


@pytest.mark.skipif(not (ROOT / "artifacts/tools/bin/helm").is_file(), reason="Helm required")
def test_ngram_chart_preserves_bounded_settings_and_rejects_reversed_window():
    value = values()
    speculation = {
        "method": "ngram",
        "num_speculative_tokens": 5,
        "prompt_lookup_min": 2,
        "prompt_lookup_max": 5,
    }
    value["engine"]["speculativeDecoding"] = speculation
    command = [
        tool("helm"),
        "template",
        "test-model",
        str(ROOT / "charts/klip-model"),
        "-n",
        "llm-d",
        "--values",
        "-",
    ]
    rendered = subprocess.run(
        command, input=yaml.safe_dump(value), text=True, capture_output=True, check=True, timeout=20
    )
    deployment = next(
        d for d in yaml.safe_load_all(rendered.stdout) if d and d["kind"] == "Deployment"
    )
    args = deployment["spec"]["template"]["spec"]["containers"][0]["args"]
    assert json.loads(args[args.index("--speculative-config") + 1]) == speculation
    assert "--no-enable-prefix-caching" in args
    speculation["method"] = "draft_model"
    assert (
        subprocess.run(
            command, input=yaml.safe_dump(value), text=True, capture_output=True, timeout=20
        ).returncode
        != 0
    )
    speculation.update(method="ngram", prompt_lookup_min=6)
    bad = subprocess.run(
        command, input=yaml.safe_dump(value), text=True, capture_output=True, timeout=20
    )
    assert bad.returncode != 0 and "lookup minimum exceeds maximum" in bad.stderr
