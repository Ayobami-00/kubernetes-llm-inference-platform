"""Fixed serving mutation tests with a synthetic Kubernetes client, never a provider."""

import copy
import json

import pytest
import retained_model
from platform_management.integrations.a10_serving import MODEL_PATH, OWNER, A10Serving
from workload_runner.experiments.a10 import BOOT_ID, INSTANCE, NODE_UID


def merge(target, patch):
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict):
            merge(target.setdefault(key, {}), value)
        else:
            target[key] = copy.deepcopy(value)


@pytest.fixture
def actuator():
    profile = retained_model.profile("baseline").model_dump(mode="json")
    args = [profile["model"]["repository"]]
    for key, value in [
        ("revision", profile["model"]["revision"]),
        ("tokenizer-revision", profile["model"]["tokenizer_revision"]),
        ("served-model-name", "gec-small"),
        ("host", "0.0.0.0"),
        ("port", "8000"),
        ("dtype", "bfloat16"),
        ("max-model-len", "768"),
        ("max-num-seqs", "8"),
        ("gpu-memory-utilization", "0.8"),
        ("tensor-parallel-size", "1"),
        ("generation-config", "vllm"),
        ("override-generation-config", '{"max_new_tokens":128}'),
        ("default-chat-template-kwargs", '{"enable_thinking":false}'),
        ("shutdown-timeout", "45"),
    ]:
        args.extend(["--" + key, value])
    args.extend(["--no-enable-prefix-caching", "--no-enable-log-requests"])
    deployment = {
        "metadata": {
            "uid": "synthetic-deployment",
            "resourceVersion": "1",
            "annotations": {"klip.dev/serving-profile": "gec-a10-retained@1"},
        },
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "template": {
                "metadata": {"annotations": {}},
                "spec": {
                    "automountServiceAccountToken": False,
                    "runtimeClassName": "nvidia",
                    "nodeSelector": {
                        "klip.dev.node-restriction.kubernetes.io/gpu-node-uid": NODE_UID
                    },
                    "containers": [
                        {
                            "name": "vllm",
                            "image": profile["engine"]["image"],
                            "command": ["vllm", "serve"],
                            "args": args,
                            "resources": {"limits": {"nvidia.com/gpu": "1"}},
                        }
                    ],
                },
            },
        },
    }
    node = {
        "metadata": {
            "uid": NODE_UID,
            "labels": {
                "klip.dev.node-restriction.kubernetes.io/gpu-ready": "true",
                "klip.dev.node-restriction.kubernetes.io/gpu-node-uid": NODE_UID,
            },
        },
        "spec": {"providerID": "lambda:///" + INSTANCE},
        "status": {
            "nodeInfo": {"bootID": BOOT_ID},
            "allocatable": {"nvidia.com/gpu": "1"},
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }
    calls = []
    pod_spec = deployment["spec"]["template"]["spec"]
    pod_spec["securityContext"] = {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    pod_spec["containers"][0]["securityContext"] = {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }

    class Client:
        def request(self, method, path, body=None, **kwargs):
            calls.append((method, path, copy.deepcopy(body)))
            if method == "GET" and path.startswith("api/v1/nodes/"):
                return copy.deepcopy(node)
            assert path == MODEL_PATH
            if method == "PATCH":
                merge(deployment, body)
            return copy.deepcopy(deployment)

        def close(self):
            pass

    return (
        A10Serving(
            {"a10_experiment_enabled": True, "a10_deployment_uid": "synthetic-deployment"}, Client()
        ),
        deployment,
        node,
        calls,
    )


def test_variant_activation_is_idempotent_and_never_restarts_ready_matching_pod(actuator):
    control, deployment, _, calls = actuator
    baseline = retained_model.profile("baseline").model_dump(mode="json")
    control.activate("op-example", baseline)
    patches = [body for method, _, body in calls if method == "PATCH"]
    assert len(patches) == 1 and "spec" not in patches[0]
    control.activate("op-example", baseline)
    assert len([c for c in calls if c[0] == "PATCH"]) == 1
    assert deployment["metadata"]["annotations"][OWNER] == "op-example"


def test_switch_changes_only_speculation_and_identity(actuator):
    control, deployment, _, calls = actuator
    before = copy.deepcopy(deployment["spec"]["template"]["spec"])
    control.activate("op-example", retained_model.profile("ngram").model_dump(mode="json"))
    after = copy.deepcopy(deployment["spec"]["template"]["spec"])
    args = after["containers"][0]["args"]
    assert json.loads(args[-1])["method"] == "ngram"
    after["containers"][0]["args"] = args[:-2]
    assert before == after
    assert all(path == MODEL_PATH or path.startswith("api/v1/nodes/") for _, path, _ in calls)


@pytest.mark.parametrize("fault", ["boot", "uid", "owner", "unknown-flag"])
def test_mutation_is_blocked_for_changed_identity_or_controls(actuator, fault):
    control, deployment, node, calls = actuator
    if fault == "boot":
        node["status"]["nodeInfo"]["bootID"] = "changed"
    elif fault == "uid":
        deployment["metadata"]["uid"] = "changed"
    elif fault == "owner":
        deployment["metadata"]["annotations"][OWNER] = "another-experiment"
    else:
        deployment["spec"]["template"]["spec"]["containers"][0]["args"].append("--arbitrary-flag")
    with pytest.raises(ValueError):
        control.activate("op-example", retained_model.profile("ngram").model_dump(mode="json"))
    assert not any(method == "PATCH" for method, _, _ in calls)
