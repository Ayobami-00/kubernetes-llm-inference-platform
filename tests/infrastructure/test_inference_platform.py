"""Controller installation rejects serving scope, mutable images and false HA evidence."""

from copy import deepcopy

import inference_platform as platform
import pytest
import yaml
from common import ROOT


@pytest.mark.skipif(
    not (ROOT / "artifacts/platform/inference/gateway-helm-v1.9.3.tgz").is_file(),
    reason="Opt-in pinned chart rendering; cached downloads are outside Git",
)
def test_pinned_controller_render_is_deterministic_and_has_no_generated_secrets():
    rendered = platform.render()
    assert rendered == platform.render()
    deployments = []
    for text in rendered.values():
        for value in yaml.safe_load_all(text):
            if value:
                assert value["kind"] not in {"Secret", "Gateway", "HTTPRoute", "InferencePool"}
                if value["kind"] == "Deployment":
                    deployments.append(value)
    assert len(deployments) == 2
    for deployment in deployments:
        assert deployment["spec"]["replicas"] == 2
        template = deployment["spec"]["template"]
        selector = template["spec"]["affinity"]["podAntiAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ][0]["labelSelector"]["matchLabels"]
        assert all(template["metadata"]["labels"][k] == v for k, v in selector.items())
    ai = platform.values()["klip-ai"]
    assert ai["controller"]["mutatingWebhook"]["certManager"]["enable"]
    assert ai["controller"]["mcp"]["sessionEncryption"]["existingSecret"]
    apis = list(yaml.safe_load_all(platform.raw_apis()))
    policies = [v for v in apis if v["kind"] != "CustomResourceDefinition"]
    assert {v["kind"] for v in policies} == {
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
    }
    assert all(v["metadata"]["name"] == "safe-upgrades.gateway.networking.k8s.io" for v in policies)
    for policy in policies:
        key = (
            "matchConstraints"
            if policy["kind"] == "ValidatingAdmissionPolicy"
            else "matchResources"
        )
        match = policy["spec"][key]
        assert match["matchPolicy"] == "Equivalent"
        assert match["namespaceSelector"] == match["objectSelector"] == {}
        assert all(rule["scope"] == "*" for rule in match["resourceRules"])
    crds = list(yaml.safe_load_all(platform.crd_bundle(rendered)))
    assert crds and all(v["kind"] == "CustomResourceDefinition" for v in crds)
    assert all(
        v["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "klip-static" for v in crds
    )


@pytest.mark.parametrize(
    "problem", ["public-serving", "mutable-image", "cpu-placement", "no-resources"]
)
def test_renderer_rejects_widened_controller_scope(problem):
    doc = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "test"},
        "spec": {
            "template": {
                "spec": {
                    "nodeSelector": platform.PLACEMENT,
                    "containers": [
                        {
                            "name": "controller",
                            "image": platform.image("gateway"),
                            "resources": {"requests": {"cpu": "200m", "memory": "256Mi"}},
                        }
                    ],
                }
            }
        },
    }
    pod = doc["spec"]["template"]["spec"]
    if problem == "public-serving":
        doc["kind"] = "Gateway"
    elif problem == "mutable-image":
        pod["containers"][0]["image"] = "envoyproxy/gateway:latest"
    elif problem == "cpu-placement":
        pod["nodeSelector"] = {}
    else:
        pod["containers"][0]["resources"] = {}
    with pytest.raises(ValueError):
        platform.validate_render({"test": yaml.safe_dump(doc)})


def pods():
    return [
        {
            "metadata": {"uid": "pod-" + str(i)},
            "spec": {
                "nodeName": "platform-0" + str(i),
                "nodeSelector": platform.PLACEMENT,
                "containers": [{"image": platform.image("gateway")}],
            },
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [{"ready": True, "imageID": platform.image("gateway")}],
            },
        }
        for i in (1, 2)
    ]


@pytest.mark.parametrize("problem", [None, "same-host", "wrong-image", "unready", "foreign-host"])
def test_native_ha_proof_requires_two_actual_pinned_ready_pods(problem):
    values = deepcopy(pods())
    if problem == "same-host":
        values[1]["spec"]["nodeName"] = "platform-01"
    elif problem == "wrong-image":
        values[1]["status"]["containerStatuses"][0]["imageID"] = "sha256:" + "a" * 64
    elif problem == "unready":
        values[1]["status"]["conditions"][0]["status"] = "False"
    elif problem == "foreign-host":
        values[1]["spec"]["nodeName"] = "unreviewed"
    if problem:
        with pytest.raises(ValueError):
            platform.verify_controller_pods(values, "gateway", {"platform-01", "platform-02"})
    else:
        assert (
            len(platform.verify_controller_pods(values, "gateway", {"platform-01", "platform-02"}))
            == 2
        )


def test_replanning_retains_original_plan_and_manifest_bytes(tmp_path, monkeypatch):
    import json

    import cluster
    import inference_security

    content = {"controller": "original fixture manifest"}
    monkeypatch.setattr(platform, "folder", lambda: tmp_path)
    monkeypatch.setattr(platform, "fetch", lambda: None)
    monkeypatch.setattr(platform, "render", lambda: dict(content))
    monkeypatch.setattr(platform, "boundary", lambda: "fixture boundary")
    monkeypatch.setattr(platform, "raw_apis", lambda: "fixture APIs")
    monkeypatch.setattr(platform, "configuration_hashes", lambda: {"fixture": "hash"})
    monkeypatch.setattr(platform, "native_inputs", lambda _: {"fixture": "cluster-inputs"})
    monkeypatch.setattr(platform, "seed_digest", lambda: "a" * 64)
    monkeypatch.setattr(inference_security, "assess", lambda _: {"scope": "synthetic-test"})
    monkeypatch.setattr(cluster, "kubectl", lambda *a: '{"metadata":{"uid":"fixture-cluster"}}')
    first = platform.prepare(tmp_path)
    directory = tmp_path / "plans" / first["approval_sha256"]
    original = (directory / "plan.json").read_bytes()
    content["controller"] = "new fixture manifest"
    second = platform.prepare(tmp_path)
    assert second["approval_sha256"] != first["approval_sha256"]
    assert (directory / "plan.json").read_bytes() == original
    assert (directory / "controller.yaml").read_text() == "original fixture manifest"
    assert json.loads((tmp_path / "plan.json").read_text()) == second


def test_extension_reader_covers_only_the_configured_namespace_watch():
    documents = [v for v in yaml.safe_load_all(platform.boundary()) if v]
    roles = [v for v in documents if v["kind"] == "Role"]
    bindings = [v for v in documents if v["kind"] == "RoleBinding"]
    assert {v["metadata"]["namespace"] for v in roles} == {
        "llm-d",
        "inference-gateway",
        "envoy-gateway-system",
    }
    assert len(bindings) == len(roles) == 3
    assert all(
        v["rules"]
        == [
            {
                "apiGroups": ["inference.networking.k8s.io"],
                "resources": ["inferencepools"],
                "verbs": ["get", "list", "watch"],
            }
        ]
        for v in roles
    )
    assert all(
        v["subjects"]
        == [
            {"kind": "ServiceAccount", "name": "envoy-gateway", "namespace": "envoy-gateway-system"}
        ]
        for v in bindings
    )
    watch = platform.values()["klip-envoy"]["config"]["envoyGateway"]["provider"]["kubernetes"][
        "watch"
    ]
    assert watch == {"type": "Namespaces", "namespaces": ["llm-d", "inference-gateway"]}
