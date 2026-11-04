"""Fixed-variant actuator. No provider, SSH, host runtime or general Helm interface."""

import copy
import json
import re
import select
import subprocess
import tempfile
from pathlib import Path

import httpx
from workload_runner.experiments.a10 import (
    BOOT_ID,
    DEPLOYMENT,
    INSTANCE,
    NODE,
    NODE_UID,
    SPECULATION,
)

from platform_management.integrations.cluster_client import ClusterClient
from platform_management.providers.base import TransientProviderError
from platform_management.services.common import digest, now

OWNER = "klip.dev/a10-experiment"
MODEL_PATH = "apis/apps/v1/namespaces/llm-d/deployments/" + DEPLOYMENT
MODEL_CLUSTER = "httproute/llm-d/writer-assist-a10/rule/0"


def ready(item):
    return not item["metadata"].get("deletionTimestamp") and any(
        c["type"] == "Ready" and c["status"] == "True"
        for c in item.get("status", {}).get("conditions", [])
    )


def without_speculation(args):
    result = list(args)
    if result.count("--speculative-config") > 1:
        raise ValueError("Duplicate speculation controls")
    if "--speculative-config" in result:
        i = result.index("--speculative-config")
        del result[i : i + 2]
    return result


def verify_controls(deployment, profile, *, allow_variant=True):
    ref = f"{profile['id']}@{profile['revision']}"
    if ref not in {"gec-a10-retained@1", "gec-a10-retained@2"} or profile["engine"][
        "speculative_decoding"
    ] != (SPECULATION if ref.endswith("@2") else None):
        raise ValueError("Only the two fixed reviewed serving variants are allowed")
    spec = deployment["spec"]
    pod = spec["template"]["spec"]
    containers = pod["containers"]
    if (
        spec["replicas"] != 1
        or spec["strategy"]["type"] != "Recreate"
        or len(containers) != 1
        or containers[0]["name"] != "vllm"
        or containers[0]["image"] != profile["engine"]["image"]
        or containers[0]["command"] != ["vllm", "serve"]
        or pod.get("automountServiceAccountToken") is not False
        or pod["nodeSelector"].get("klip.dev.node-restriction.kubernetes.io/gpu-node-uid")
        != NODE_UID
        or pod.get("runtimeClassName") != "nvidia"
        or containers[0]["resources"]["limits"].get("nvidia.com/gpu") != "1"
        and containers[0]["resources"]["limits"].get("nvidia.com/gpu") != 1
    ):
        raise ValueError("Live serving controls differ from the reviewed single-A10 deployment")
    args = containers[0]["args"]
    security = pod.get("securityContext", {})
    container_security = containers[0].get("securityContext", {})
    if (
        any(pod.get(key) for key in ("hostNetwork", "hostPID", "hostIPC", "initContainers"))
        or any("hostPath" in volume for volume in pod.get("volumes", []))
        or security.get("runAsNonRoot") is not True
        or security.get("runAsUser") != 10001
        or security.get("seccompProfile", {}).get("type") != "RuntimeDefault"
        or container_security.get("allowPrivilegeEscalation") is not False
        or container_security.get("readOnlyRootFilesystem") is not True
        or container_security.get("privileged")
        or container_security.get("capabilities", {}).get("drop") != ["ALL"]
        or container_security.get("capabilities", {}).get("add")
    ):
        raise ValueError("Serving isolation differs from the bounded image-risk review")
    expected = {
        "--revision": profile["model"]["revision"],
        "--tokenizer-revision": profile["model"]["tokenizer_revision"],
        "--served-model-name": "gec-small",
        "--dtype": "bfloat16",
        "--max-model-len": "768",
        "--max-num-seqs": "8",
        "--gpu-memory-utilization": "0.8",
        "--tensor-parallel-size": "1",
        "--host": "0.0.0.0",
        "--port": "8000",
        "--generation-config": "vllm",
        "--shutdown-timeout": "45",
    }
    if args[0] != profile["model"]["repository"] or any(
        args.count(k) != 1 or args[args.index(k) + 1] != v for k, v in expected.items()
    ):
        raise ValueError("Model pins or engine arguments changed outside this experiment")
    if "--no-enable-prefix-caching" not in args or "--no-enable-log-requests" not in args:
        raise ValueError("Prefix caching or ordinary request logging controls differ")
    if len(without_speculation(args)) != 31:
        raise ValueError("Unexpected extra or missing serving arguments")
    if json.loads(args[args.index("--default-chat-template-kwargs") + 1]) != {
        "enable_thinking": False
    }:
        raise ValueError("Non-thinking chat settings differ")
    if json.loads(args[args.index("--override-generation-config") + 1]) != {"max_new_tokens": 128}:
        raise ValueError("Output cap differs")
    if not allow_variant:
        observed = (
            json.loads(args[args.index("--speculative-config") + 1])
            if "--speculative-config" in args
            else None
        )
        if observed != profile["engine"]["speculative_decoding"]:
            raise ValueError("Effective speculative arguments differ")


class A10Serving:
    def __init__(self, binding, client=None):
        if not binding.get("a10_experiment_enabled"):
            raise ValueError("The fixed A10 actuator is disabled")
        self.binding = binding
        self.client = client or ClusterClient(binding)

    def close(self):
        self.client.close()

    def deployment(self):
        value = self.client.request("GET", MODEL_PATH)
        if (
            self.binding.get("a10_deployment_uid")
            and value["metadata"]["uid"] != self.binding["a10_deployment_uid"]
        ):
            raise ValueError("Retained serving deployment UID changed")
        return value

    def node(self):
        node = self.client.request("GET", "api/v1/nodes/" + NODE)
        labels = node["metadata"].get("labels", {})
        if (
            node["metadata"]["uid"] != NODE_UID
            or node["status"]["nodeInfo"]["bootID"] != BOOT_ID
            or node["spec"].get("providerID") != "lambda:///" + INSTANCE
            or labels.get("klip.dev.node-restriction.kubernetes.io/gpu-ready") != "true"
            or labels.get("klip.dev.node-restriction.kubernetes.io/gpu-node-uid") != NODE_UID
            or node["status"].get("allocatable", {}).get("nvidia.com/gpu") != "1"
            or not ready(node)
            or any(t["key"] == "klip.dev/bootstrap" for t in node["spec"].get("taints", []))
        ):
            raise ValueError("Retained node, boot, provider identity or GPU readiness changed")
        return node

    def client_node(self, expected):
        node = self.client.request("GET", "api/v1/nodes/" + expected["name"])
        if (
            node["metadata"]["uid"] != expected["uid"]
            or node["metadata"]["labels"].get("klip.dev.node-restriction.kubernetes.io/platform")
            != "true"
            or "node-role.kubernetes.io/control-plane" in node["metadata"]["labels"]
            or node["spec"].get("unschedulable")
            or not ready(node)
            or int(node["status"].get("allocatable", {}).get("nvidia.com/gpu", 0)) != 0
        ):
            raise ValueError("Pinned CPU workload-generator placement is unavailable or changed")
        return {
            "name": node["metadata"]["name"],
            "uid": node["metadata"]["uid"],
            "boot_id": node["status"]["nodeInfo"]["bootID"],
            "observed_at": now().isoformat(),
        }

    def activate(self, owner, profile):
        self.node()
        deployment = self.deployment()
        verify_controls(deployment, profile)
        annotations = deployment["metadata"].get("annotations", {})
        if annotations.get(OWNER) not in {None, owner}:
            raise ValueError("Serving deployment belongs to another experiment")
        current_ref = annotations.get("klip.dev/serving-profile")
        if current_ref not in {"gec-a10-retained@1", "gec-a10-retained@2"}:
            raise ValueError("Serving profile is outside the fixed variant allowlist")
        desired_ref = f"{profile['id']}@{profile['revision']}"
        if current_ref == desired_ref:
            verify_controls(deployment, profile, allow_variant=False)
        patch = {
            "metadata": {
                "resourceVersion": deployment["metadata"]["resourceVersion"],
                "annotations": {OWNER: owner},
            }
        }
        if current_ref != desired_ref:
            containers = copy.deepcopy(deployment["spec"]["template"]["spec"]["containers"])
            args = without_speculation(containers[0]["args"])
            speculation = profile["engine"]["speculative_decoding"]
            if speculation:
                args.extend(["--speculative-config", json.dumps(speculation, sort_keys=True)])
            containers[0]["args"] = args
            identity = {
                "klip.dev/serving-profile": desired_ref,
                "klip.dev/serving-profile-sha256": digest(profile),
            }
            patch["metadata"]["annotations"].update(identity)
            patch["spec"] = {
                "template": {
                    "metadata": {"annotations": identity},
                    "spec": {"containers": containers},
                }
            }
        if annotations.get(OWNER) != owner or current_ref != desired_ref:
            changed = self.client.request("PATCH", MODEL_PATH, patch, conflict=True)
            if changed is None:
                raise TransientProviderError(
                    "Serving resource changed concurrently; observe before mutation"
                )
            deployment = changed
        return deployment

    def observe(self, owner, profile):
        node = self.node()
        deployment = self.deployment()
        if deployment["metadata"].get("annotations", {}).get(OWNER) != owner:
            raise ValueError("Experiment lost serving ownership")
        verify_controls(deployment, profile, allow_variant=False)
        status = deployment.get("status", {})
        pods = self.client.request(
            "GET",
            "api/v1/namespaces/llm-d/pods?labelSelector=klip.dev%2Fserving-deployment%3Dwriter-assist-a10",
        )["items"]
        active = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
        for pod in active:
            if any(
                c.get("restartCount", 0) for c in pod.get("status", {}).get("containerStatuses", [])
            ):
                # Stop an unsafe kubelet restart loop; never restart the VM or retry the experiment.
                self.client.request(
                    "PATCH",
                    MODEL_PATH,
                    {
                        "metadata": {"resourceVersion": deployment["metadata"]["resourceVersion"]},
                        "spec": {"replicas": 0},
                    },
                )
                raise ValueError(
                    "Model container restarted; deployment stopped and evidence retained"
                )
        is_ready = (
            status.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
            and status.get("readyReplicas") == 1
            and len(active) == 1
            and ready(active[0])
            and active[0]["spec"].get("nodeName") == NODE
        )
        if is_ready and not any(
            c["name"] == "vllm"
            and c.get("imageID", "").endswith(profile["engine"]["image"].split("@", 1)[1])
            for c in active[0]["status"].get("containerStatuses", [])
        ):
            raise ValueError("Observed model container digest differs from the reviewed pin")
        return is_ready, {
            "observed_at": now().isoformat(),
            "node_uid": node["metadata"]["uid"],
            "boot_id": BOOT_ID,
            "deployment_uid": deployment["metadata"]["uid"],
            "generation": deployment["metadata"]["generation"],
            "pods": [
                {"uid": p["metadata"]["uid"], "name": p["metadata"]["name"], "ready": ready(p)}
                for p in active
            ],
        }

    def release(self, owner):
        deployment = self.deployment()
        if deployment["metadata"].get("annotations", {}).get(OWNER) == owner:
            deployment = self.client.request(
                "PATCH",
                MODEL_PATH,
                {
                    "metadata": {
                        "resourceVersion": deployment["metadata"]["resourceVersion"],
                        "annotations": {OWNER: None},
                    }
                },
            )
        return deployment

    def gateway(self):
        """Use the worker's limited SA through a loopback-only admin port-forward."""
        epp = self.client.request(
            "GET", "apis/apps/v1/namespaces/llm-d/deployments/writer-assist-a10-epp"
        )
        epp_image = (
            "ghcr.io/llm-d/llm-d-router-endpoint-picker:v1.0.0@sha256:"
            "8c6b24302ed21a1c4aa6ba2306187236519a8fdc92282998b33e65c6a446c516"
        )
        service = self.client.request("GET", "api/v1/namespaces/llm-d/services/inference")
        if service["spec"].get("type", "ClusterIP") != "ClusterIP" or service["spec"].get(
            "externalIPs"
        ):
            raise ValueError("The experimental gateway must remain ClusterIP-only")
        if (
            epp.get("status", {}).get("readyReplicas") != 1
            or epp.get("status", {}).get("observedGeneration", 0) < epp["metadata"]["generation"]
            or [c["image"] for c in epp["spec"]["template"]["spec"]["containers"]] != [epp_image]
        ):
            raise ValueError("The pinned llm-d endpoint picker is not ready")
        pods = self.client.request(
            "GET",
            "api/v1/namespaces/llm-d/pods?labelSelector=gateway.envoyproxy.io%2Fowning-gateway-name%3Dinference",
        )["items"]
        pods = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
        if (
            len(pods) != 2
            or len({p["spec"].get("nodeName") for p in pods}) != 2
            or not all(ready(p) for p in pods)
        ):
            raise ValueError("Two ready gateway replicas on distinct CPU hosts are required")
        rows = []
        for pod in pods:
            rows.append(self.gateway_replica(pod))
        return {
            "observed_at": now().isoformat(),
            "replicas": rows,
            "endpoint_picker_uid": epp["metadata"]["uid"],
            "endpoint_picker_image": epp_image,
        }

    def gateway_replica(self, pod):
        name = pod["metadata"]["name"]
        if not re.fullmatch(r"inference-[a-z0-9-]+", name):
            raise ValueError("Unexpected gateway Pod")
        proxy = next(c for c in pod["status"]["containerStatuses"] if c["name"] == "envoy")
        if not proxy["imageID"].endswith(
            "sha256:00c38048aeb4bb4b49d9ec6f3d7742a7c9f3d4d33f0371632a76734c02bebb7d"
        ):
            raise ValueError("Gateway proxy differs from the reviewed image")
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config"
            config.write_text(
                json.dumps(
                    {
                        "apiVersion": "v1",
                        "kind": "Config",
                        "current-context": "worker",
                        "contexts": [
                            {"name": "worker", "context": {"cluster": "cluster", "user": "worker"}}
                        ],
                        "clusters": [
                            {
                                "name": "cluster",
                                "cluster": {
                                    "server": self.client.client.base_url.__str__().rstrip("/"),
                                    "certificate-authority": self.binding["kube_ca_file"],
                                },
                            }
                        ],
                        "users": [
                            {
                                "name": "worker",
                                "user": {"tokenFile": self.binding["kube_token_file"]},
                            }
                        ],
                    }
                )
            )
            config.chmod(0o600)
            process = subprocess.Popen(
                [
                    "kubectl",
                    "--kubeconfig",
                    str(config),
                    "--cache-dir",
                    str(Path(temporary) / "cache"),
                    "-n",
                    "llm-d",
                    "port-forward",
                    "--address=127.0.0.1",
                    "pod/" + name,
                    ":19000",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            try:
                if not select.select([process.stdout], [], [], 15)[0]:
                    raise ValueError("Gateway admin observation timed out")
                line = process.stdout.readline()
                match = re.match(r"Forwarding from 127\.0\.0\.1:(\d+) -> 19000", line)
                if not match:
                    raise ValueError("Gateway admin tunnel could not be established")
                with httpx.Client(trust_env=False, timeout=15) as client:
                    response = client.get(
                        f"http://127.0.0.1:{match[1]}/config_dump?resource=dynamic_active_clusters"
                    )
                    response.raise_for_status()
                    config_dump = response.json()
                matches = []

                def visit(value):
                    if isinstance(value, dict):
                        if value.get("name") == MODEL_CLUSTER:
                            matches.append(value)
                        else:
                            for child in value.values():
                                visit(child)
                    elif isinstance(value, list):
                        for child in value:
                            visit(child)

                visit(config_dump)
                if len(matches) != 1:
                    raise ValueError("Fixed WriterAssist upstream was not found exactly once")
                options = (
                    matches[0]
                    .get("typed_extension_protocol_options", {})
                    .get("envoy.extensions.upstreams.http.v3.HttpProtocolOptions", {})
                )
                if options.get("common_http_protocol_options", {}).get("idle_timeout") != "3s":
                    raise ValueError("Effective gateway upstream idle timeout is not three seconds")
                return {
                    "pod": name,
                    "pod_uid": pod["metadata"]["uid"],
                    "node": pod["spec"]["nodeName"],
                    "http_idle_timeout": "3s",
                }
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
