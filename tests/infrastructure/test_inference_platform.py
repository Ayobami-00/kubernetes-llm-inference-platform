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
