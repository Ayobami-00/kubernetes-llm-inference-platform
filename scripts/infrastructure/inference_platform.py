"""Install only the pinned inference controllers/CRDs; model and gateway releases stay separate."""

import argparse
import fcntl
import hashlib
import json
import secrets
import subprocess
from pathlib import Path

import yaml
from common import ROOT, private_dir, source_digest, state_home, tool, utc_now, write_private
from inference_artifacts import CACHE, fetch, settings

RELEASES = {
    "klip-gateway-crds": ("gateway-crds", "envoy-gateway-system"),
    "klip-ai-crds": ("ai-crds", "envoy-ai-gateway-system"),
    "klip-envoy": ("gateway", "envoy-gateway-system"),
    "klip-ai": ("ai", "envoy-ai-gateway-system"),
}
PLACEMENT = {"klip.dev.node-restriction.kubernetes.io/platform": "true"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def image(component):
    lock = settings()
    return lock["images"][lock["scan_components"][component]]["image"]


def spreading(labels):
    return {
        "podAntiAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": [
                {"topologyKey": "kubernetes.io/hostname", "labelSelector": {"matchLabels": labels}}
            ]
        }
    }


def values():
    # Keep the two immutable upstream integration documents as the starting point.
    gateway = yaml.safe_load((CACHE / "envoy-ai-integration.yaml").read_text())
    extension = gateway["config"]["envoyGateway"]["extensionManager"]
    extension["service"]["fqdn"]["hostname"] = (
        "ai-gateway-controller.envoy-ai-gateway-system.svc.cluster.local"
    )
    extension["backendResources"] = [
        {"group": "inference.networking.k8s.io", "kind": "InferencePool", "version": "v1"}
    ]
    gateway["config"]["envoyGateway"]["provider"] = {
        "type": "Kubernetes",
        "kubernetes": {"deploy": {"type": "GatewayNamespace"}},
    }
    gateway.update(
        crds={"enabled": False},
        deployment={
            "replicas": 2,
            "envoyGateway": {
                "resources": {
                    "requests": {"cpu": "200m", "memory": "256Mi"},
                    "limits": {"cpu": "1", "memory": "768Mi"},
                }
            },
            "pod": {
                "nodeSelector": PLACEMENT,
                "affinity": spreading({"app.kubernetes.io/name": "gateway-helm"}),
            },
        },
        podDisruptionBudget={"minAvailable": 1},
        certgen={
            "job": {
                "nodeSelector": PLACEMENT,
                "resources": {
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "1", "memory": "256Mi"},
                },
            }
        },
    )
    gateway["global"] = {
        "images": {
            "envoyGateway": {"image": image("gateway")},
            "envoyProxy": {"image": image("proxy")},
        }
    }
    ai = {
        "controller": {
            "replicaCount": 2,
            "logFormat": "json",
            "logLevel": "info",
            "image": {"tag": "v1.2.0@" + image("ai-controller").split("@")[1]},
            "leaderElection": {"enabled": True},
            "watch": {"namespaces": ["llm-d", "inference-gateway"]},
            "nodeSelector": PLACEMENT,
            "affinity": spreading({"app.kubernetes.io/name": "ai-gateway-helm"}),
            "podDisruptionBudget": {"enabled": True, "minAvailable": 1},
            "resources": {
                "requests": {"cpu": "200m", "memory": "256Mi"},
                "limits": {"cpu": "1", "memory": "768Mi"},
            },
            "extraEnvVars": [{"name": "DISABLE_PPROF", "value": "true"}],
            "spanRequestHeaderAttributes": "",
            "logRequestHeaderAttributes": "",
            "mutatingWebhook": {"certManager": {"enable": True}},
            "mcp": {"sessionEncryption": {"existingSecret": "klip-ai-mcp-seed"}},
        },
        "extProc": {
            "image": {"tag": "v1.2.0@" + image("ai-extproc").split("@")[1]},
            "logFormat": "json",
            "logLevel": "info",
            "enableRedaction": True,
            "extraEnvVars": [
                {"name": "DISABLE_PPROF", "value": "true"},
                {"name": "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "value": "false"},
            ],
        },
    }
    return {
        "klip-gateway-crds": {
            "crds": {"envoyGateway": {"enabled": True}, "gatewayAPI": {"enabled": False}}
        },
        "klip-ai-crds": {},
        "klip-envoy": gateway,
        "klip-ai": ai,
    }


def boundary():
    documents = []
    for namespace in ("envoy-gateway-system", "envoy-ai-gateway-system"):
        documents.append(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": namespace,
                    "labels": {
                        "app.kubernetes.io/part-of": "klip",
                        "pod-security.kubernetes.io/enforce": "restricted",
                        "pod-security.kubernetes.io/enforce-version": "v1.36",
                    },
                },
            }
        )
        documents.append(
            {
                "apiVersion": "cilium.io/v2",
                "kind": "CiliumNetworkPolicy",
                "metadata": {"name": "inference-controllers", "namespace": namespace},
                "spec": {
                    "endpointSelector": {},
                    "ingress": [
                        {
                            "fromEntities": ["kube-apiserver"],
                            "toPorts": [
                                {
                                    "ports": [
                                        {"port": "9443", "protocol": "TCP"},
                                    ]
                                }
                            ],
                        },
                        {
                            "fromEndpoints": [
                                {"matchLabels": {"k8s:io.kubernetes.pod.namespace": n}}
                                for n in (
                                    "envoy-gateway-system",
                                    "envoy-ai-gateway-system",
                                    "inference-gateway",
                                )
                            ],
                            "toPorts": [
                                {
                                    "ports": [
                                        {"port": str(p), "protocol": "TCP"}
                                        for p in (1063, 18000, 18002)
                                    ]
                                }
                            ],
                        },
                        {
                            "fromEndpoints": [
                                {
                                    "matchLabels": {
                                        "k8s:io.kubernetes.pod.namespace": "observability",
                                        "app.kubernetes.io/name": "prometheus",
                                    }
                                }
                            ],
                            "toPorts": [
                                {
                                    "ports": [
                                        {"port": str(p), "protocol": "TCP"} for p in (8080, 19001)
                                    ]
                                }
                            ],
                        },
                    ],
                    "egress": [
                        {
                            "toEntities": ["kube-apiserver"],
                            "toPorts": [
                                {
                                    "ports": [
                                        {"port": "443", "protocol": "TCP"},
                                        {"port": "6443", "protocol": "TCP"},
                                    ]
                                }
                            ],
                        },
                        {
                            "toEndpoints": [
                                {
                                    "matchLabels": {
                                        "k8s:io.kubernetes.pod.namespace": "kube-system",
                                        "k8s:k8s-app": "kube-dns",
                                    }
                                }
                            ],
                            "toPorts": [
                                {
                                    "ports": [
                                        {"port": "53", "protocol": "UDP"},
                                        {"port": "53", "protocol": "TCP"},
                                    ]
                                }
                            ],
                        },
                        {
                            "toEndpoints": [
                                {"matchLabels": {"k8s:io.kubernetes.pod.namespace": n}}
                                for n in ("envoy-gateway-system", "envoy-ai-gateway-system")
                            ],
                            "toPorts": [
                                {
                                    "ports": [
                                        {"port": str(p), "protocol": "TCP"}
                                        for p in (1063, 18000, 18002)
                                    ]
                                }
                            ],
                        },
                    ],
                },
            }
        )
    return yaml.safe_dump_all(documents, sort_keys=True)


def render():
    lock = settings()
    result = {}
    for name, (key, namespace) in RELEASES.items():
        chart = lock["charts"][key]
        result[name] = subprocess.check_output(
            [
                tool("helm"),
                "template",
                name,
                str(CACHE / chart["filename"]),
                "-n",
                namespace,
                "--kube-version",
                "1.36.5",
                "--values",
                "-",
            ],
            input=yaml.safe_dump(values()[name]),
            text=True,
            timeout=60,
        )
    validate_render(result)
    return result


def validate_render(rendered):
    allowed = {image(c) for c in ("gateway", "proxy", "ai-controller", "ai-extproc")}

    # Some upstream chart fields concatenate a tag before the immutable digest.
    def pinned(reference):
        repository, digest = reference.split("@")
        return (
            repository.rsplit(":", 1)[0] + "@" + digest
            if ":" in repository.rsplit("/", 1)[-1]
            else reference
        )

    for text in rendered.values():
        for document in yaml.safe_load_all(text):
            if not document:
                continue
            if document["kind"] in {
                "Gateway",
                "HTTPRoute",
                "AIGatewayRoute",
                "InferencePool",
                "ServiceMonitor",
                "Secret",
            }:
                raise ValueError(
                    "Controller installation cannot deploy serving or monitoring resources"
                )
            if (
                document["kind"] == "Service"
                and document["spec"].get("type", "ClusterIP") != "ClusterIP"
            ):
                raise ValueError("Inference controller Services must remain private")
            if document["kind"] not in {"Job", "Deployment"}:
                continue
            pod = document["spec"]["template"]["spec"]
            if pod.get("nodeSelector") != PLACEMENT or pod.get("hostNetwork"):
                raise ValueError("Inference controllers must use protected CPU capacity")
            for container in pod.get("containers", []) + pod.get("initContainers", []):
                if (
                    "@sha256:" not in container["image"]
                    or pinned(container["image"]) not in allowed
                ):
                    raise ValueError("Unreviewed inference controller image")
                if not container.get("resources", {}).get("requests"):
                    raise ValueError("Inference controller resources must be reserved")


def configuration_hashes():
    return {
        "values": sha(yaml.safe_dump(values(), sort_keys=True).encode()),
        "boundary": sha(boundary().encode()),
        "lock": sha((ROOT / "deploy/inference.lock.json").read_bytes()),
    }


def raw_apis():
    documents = []
    for name in ("gateway-api", "inference-api"):
        for document in yaml.safe_load_all((CACHE / (name + ".yaml")).read_text()):
            if not document:
                continue
            if document["kind"] != "CustomResourceDefinition" and not (
                name == "gateway-api"
                and document["kind"]
                in {"ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"}
                and document["metadata"]["name"] == "safe-upgrades.gateway.networking.k8s.io"
            ):
                raise ValueError(
                    "Only locked APIs and their exact upgrade safety policy are permitted"
                )
            document["metadata"].setdefault("labels", {}).update(
                {
                    "app.kubernetes.io/part-of": "klip",
                    "klip.dev/platform-component": "inference",
                }
            )
            documents.append(document)
    return yaml.safe_dump_all(documents, sort_keys=True)


def crd_bundle(rendered):
    # These upstream schemas exceed Helm's 1 MiB release-Secret limit. Keep
    # their checked chart bytes, but give static CRDs one explicit SSA owner.
    documents = []
    for release in ("klip-gateway-crds", "klip-ai-crds"):
        for document in yaml.safe_load_all(rendered[release]):
            if not document:
                continue
            if document["kind"] != "CustomResourceDefinition":
                raise ValueError("CRD bundle contains a non-CRD resource")
            document["metadata"].setdefault("labels", {}).update(
                {
                    "app.kubernetes.io/part-of": "klip",
                    "app.kubernetes.io/managed-by": "klip-static",
                    "klip.dev/platform-component": "inference",
                }
            )
            documents.append(document)
    return yaml.safe_dump_all(documents, sort_keys=True)


def native_inputs(kubectl):
    from image_credentials import healthy_targets

    namespaces = json.loads(kubectl("get", "namespaces", "-o", "json"))["items"]
    existing = {v["metadata"]["name"]: v["metadata"]["uid"] for v in namespaces}
    crds = {}
    for value in json.loads(kubectl("get", "crds", "-o", "json"))["items"]:
        if value["spec"]["group"] not in {
            "gateway.networking.k8s.io",
            "gateway.envoyproxy.io",
            "aigateway.envoyproxy.io",
            "inference.networking.k8s.io",
        }:
            continue
        metadata = value["metadata"]
        if (
            metadata.get("annotations", {}).get("meta.helm.sh/release-name") not in RELEASES
            and metadata.get("labels", {}).get("klip.dev/platform-component") != "inference"
        ):
            raise ValueError("Existing inference CRD belongs to a different owner")
        crds[metadata["name"]] = {
            "uid": metadata["uid"],
            "spec_sha256": sha(json.dumps(value["spec"], sort_keys=True).encode()),
        }
    # This temporary review is deliberately controller-only. Once serving exists,
    # an upgrade needs a review that actually covers that active data plane.
    for name in (
        "gateways.gateway.networking.k8s.io",
        "inferencepools.inference.networking.k8s.io",
        "aigatewayroutes.aigateway.envoyproxy.io",
    ):
        if name in crds and json.loads(kubectl("get", name, "-A", "-o", "json"))["items"]:
            raise ValueError("An active serving resource is outside this private-controller review")
    certificates = {}
    for value in json.loads(kubectl("-n", "cert-manager", "get", "deployments", "-o", "json"))[
        "items"
    ]:
        if value["metadata"]["name"] not in {
            "cert-manager",
            "cert-manager-webhook",
            "cert-manager-cainjector",
        }:
            raise ValueError("Unexpected certificate-controller deployment")
        if value.get("status", {}).get("readyReplicas", 0) < 2:
            raise ValueError("The existing certificate foundation is not HA-ready")
        certificates[value["metadata"]["name"]] = {
            "uid": value["metadata"]["uid"],
            "spec_sha256": sha(json.dumps(value["spec"], sort_keys=True).encode()),
        }
    if len(certificates) != 3:
        raise ValueError("The pinned certificate foundation is missing")
    policies = {}
    for kind in ("validatingadmissionpolicy", "validatingadmissionpolicybinding"):
        text = kubectl(
            "get",
            kind,
            "safe-upgrades.gateway.networking.k8s.io",
            "--ignore-not-found",
            "-o",
            "json",
        )
        if text.strip():
            policy = json.loads(text)
            if (
                policy["metadata"].get("labels", {}).get("klip.dev/platform-component")
                != "inference"
            ):
                raise ValueError("Gateway API upgrade policy belongs to a different owner")
            policies[kind] = {
                "uid": policy["metadata"]["uid"],
                "spec_sha256": sha(json.dumps(policy["spec"], sort_keys=True).encode()),
            }
    return {
        "namespaces": {
            n: existing.get(n)
            for n in ("kube-system", "llm-d", "envoy-gateway-system", "envoy-ai-gateway-system")
        },
        "crds": crds,
        "certificate_deployments": certificates,
        "api_upgrade_policies": policies,
        "platform_nodes": healthy_targets(),
    }


def seed_file():
    return private_dir(state_home() / "credentials") / "inference-mcp-seed"


def seed_digest():
    path = seed_file()
    if not path.exists():
        # Helm otherwise generates a different secret on every template render.
        write_private(path, secrets.token_urlsafe(48))
    if path.is_symlink() or path.stat().st_mode & 0o077 or len(path.read_bytes()) < 64:
        raise ValueError("The private inference seed file is not safely staged")
    return sha(path.read_bytes())


def stage_seed(cp, expected):
    import base64

    if seed_digest() != expected:
        raise ValueError("The staged controller secret changed after planning")
    command = [
        "sudo",
        "kubectl",
        "--kubeconfig=/etc/kubernetes/admin.conf",
        "-n",
        "envoy-ai-gateway-system",
    ]
    present = cp.run(
        [*command, "get", "secret", "klip-ai-mcp-seed", "--ignore-not-found", "-o", "json"],
        secret=True,
    )
    seed = seed_file().read_bytes()
    if present.strip():
        value = json.loads(present)
        if value.get("type") != "Opaque" or base64.b64decode(value["data"]["seed"]) != seed:
            raise ValueError("An existing controller secret differs; never replace it implicitly")
    else:
        value = json.loads(
            cp.run(
                [*command, "create", "-f", "-", "-o", "json"],
                data=json.dumps(
                    {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": {
                            "name": "klip-ai-mcp-seed",
                            "labels": {"app.kubernetes.io/part-of": "klip"},
                        },
                        "type": "Opaque",
                        "data": {"seed": base64.b64encode(seed).decode()},
                    }
                ),
                secret=True,
            )
        )
    return {
        "name": "klip-ai-mcp-seed",
        "uid": value["metadata"]["uid"],
        "sha256": expected,
        "recovery": "Private seed retained locally; encrypted external recovery is pending.",
    }


def folder():
    return private_dir(state_home() / "cluster/inference-platform")


def verify_controller_pods(pods, component, allowed_nodes):
    expected = image(component)
    if len(pods) != 2 or len({p["spec"].get("nodeName") for p in pods}) != 2:
        raise ValueError("Inference controller replicas must be spread across two CPU hosts")
    observed = []
    for pod in pods:
        containers = pod["spec"].get("containers", [])
        statuses = pod.get("status", {}).get("containerStatuses", [])
        if (
            pod["spec"].get("nodeName") not in allowed_nodes
            or pod["spec"].get("nodeSelector") != PLACEMENT
            or len(containers) != 1
            or not containers[0]["image"].endswith("@" + expected.split("@")[1])
            or len(statuses) != 1
            or not statuses[0].get("ready")
            or not statuses[0].get("imageID", "").endswith(expected.split("@")[1])
            or not any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in pod.get("status", {}).get("conditions", [])
            )
        ):
            raise ValueError("Inference controller runtime image, readiness or placement differs")
        observed.append(
            {
                "uid": pod["metadata"]["uid"],
                "node": pod["spec"]["nodeName"],
                "image_id": statuses[0]["imageID"],
            }
        )
    return observed


def prepare(scans):
    from cluster import kubectl
    from inference_security import assess

    fetch()
    rendered = render()
    assessment = assess(scans)
    identity = json.loads(kubectl("get", "namespace", "kube-system", "-o", "json"))["metadata"][
        "uid"
    ]
    root = folder()
    for name, text in rendered.items():
        write_private(root / (name + ".yaml"), text)
    write_private(root / "boundary.yaml", boundary())
    write_private(root / "apis.yaml", raw_apis())
    plan = {
        "schema_version": "klip-inference-controller-plan.v1",
        "created_at": utc_now(),
        "source_sha256": source_digest(),
        "cluster_uid": identity,
        "configuration": configuration_hashes(),
        "native_inputs": native_inputs(kubectl),
        "seed_sha256": seed_digest(),
        "security": assessment,
        "scan_directory": str(scans.resolve()),
        "rendered": {name: sha(text.encode()) for name, text in rendered.items()},
        "apis_sha256": sha(raw_apis().encode()),
        "limitations": [
            "Only controllers and CRDs; no model, public gateway or inference readiness claim."
        ],
    }
    approval = sha(json.dumps(plan, sort_keys=True).encode())
    plan["approval_sha256"] = approval
    write_private(root / "plan.json", plan)
    return plan


def deploy(approval):
    from cluster import FOLDER, Host, checked, kubeconfigs, kubectl, load_hosts, tunnel
    from inference_security import assess

    root = folder()
    plan = json.loads((root / "plan.json").read_text())
    if (
        approval != plan["approval_sha256"]
        or approval
        != sha(
            json.dumps(
                {k: v for k, v in plan.items() if k != "approval_sha256"}, sort_keys=True
            ).encode()
        )
        or plan["source_sha256"] != source_digest()
        or plan["configuration"] != configuration_hashes()
        or plan["native_inputs"] != native_inputs(kubectl)
        or plan["security"] != assess(Path(plan["scan_directory"]))
        or plan["seed_sha256"] != seed_digest()
        or plan["apis_sha256"] != sha(raw_apis().encode())
        or plan["rendered"] != {k: sha(v.encode()) for k, v in render().items()}
    ):
        raise ValueError("Inference controller inputs changed after review")
    receipt_path = root / (approval + ".result.json")
    if receipt_path.exists():
        raise ValueError("Observe/reconcile the original controller installation before replanning")
    receipt = {"approval_sha256": approval, "status": "running", "started_at": utc_now()}
    write_private(receipt_path, receipt)
    cp = Host(load_hosts()["cp-01"])
    command = ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf"]
    try:
        cp.run([*command, "apply", "-f", "-"], data=boundary())
        cp.run(
            [
                *command,
                "apply",
                "--server-side",
                "--field-manager=klip-inference-foundation",
                "-f",
                "-",
            ],
            data=raw_apis(),
        )
        cp.run(
            [
                *command,
                "apply",
                "--server-side",
                "--field-manager=klip-inference-foundation",
                "-f",
                "-",
            ],
            data=crd_bundle(render()),
        )
        receipt["credential"] = stage_seed(cp, plan["seed_sha256"])
        write_private(receipt_path, receipt)
        kubeconfigs()
        with tunnel():
            for name, (key, namespace) in RELEASES.items():
                if key in {"gateway-crds", "ai-crds"}:
                    continue
                path = root / (name + ".values.yaml")
                write_private(path, yaml.safe_dump(values()[name]))
                output = checked(
                    [
                        tool("helm"),
                        "upgrade",
                        "--install",
                        name,
                        str(CACHE / settings()["charts"][key]["filename"]),
                        "-n",
                        namespace,
                        "--kubeconfig",
                        str(FOLDER / "operator.conf"),
                        "--values",
                        str(path),
                        "--atomic",
                        "--wait",
                        "--timeout",
                        "5m",
                        "--history-max",
                        "5",
                    ],
                    timeout=360,
                )
                write_private(root / (name + ".helm.log"), output)
        observed = {}
        for namespace, name, component in (
            ("envoy-gateway-system", "envoy-gateway", "gateway"),
            ("envoy-ai-gateway-system", "ai-gateway-controller", "ai-controller"),
        ):
            value = json.loads(kubectl("-n", namespace, "get", "deployment", name, "-o", "json"))
            if value.get("status", {}).get("readyReplicas", 0) != 2:
                raise ValueError("Both inference controller replicas are not ready")
            observed[name] = {
                "uid": value["metadata"]["uid"],
                "generation": value["metadata"]["generation"],
                "ready_replicas": 2,
                "expected_image": image(component),
            }
            release = "klip-envoy" if component == "gateway" else "klip-ai"
            pods = json.loads(
                kubectl(
                    "-n",
                    namespace,
                    "get",
                    "pods",
                    "-l",
                    "app.kubernetes.io/instance=" + release,
                    "-o",
                    "json",
                )
            )["items"]
            observed[name]["pods"] = verify_controller_pods(
                pods, component, plan["native_inputs"]["platform_nodes"]
            )
        receipt.update(
            status="deployed",
            completed_at=utc_now(),
            controllers=observed,
            limitations=plan["limitations"],
        )
    except Exception as error:
        receipt.update(status="reconciliation-required", error_type=type(error).__name__)
        raise
    finally:
        write_private(receipt_path, receipt)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["render", "prepare", "deploy"])
    parser.add_argument("--scans", type=Path)
    parser.add_argument("--approve-plan")
    args = parser.parse_args()
    with (private_dir(state_home() / "cluster") / "operator.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        fetch()
        if args.action == "render":
            print(boundary() + "\n---\n" + "\n---\n".join(render().values()))
        elif args.action == "prepare":
            if args.scans is None:
                parser.error("prepare requires --scans")
            print(json.dumps(prepare(args.scans), indent=2))
        else:
            print(json.dumps(deploy(args.approve_plan), indent=2))
