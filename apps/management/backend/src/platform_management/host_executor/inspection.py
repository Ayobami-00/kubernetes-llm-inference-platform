"""Admit one quarantined A10 only after real host, CUDA, network and scrape proof."""

import hashlib
import json
import time
from datetime import UTC, datetime

from cluster_models import assert_node_identity
from common import utc_now, write_private
from gpu_operator import BOOTSTRAP, CANDIDATE, image, settings, values
from gpu_runtime import observe_kernel
from platform_contracts import HostTaskObservation
from platform_contracts.inventory import GPUCapacityObservation

from platform_management.host_executor.cleanup import checked_peer, kubectl
from platform_management.host_executor.gpu_approval import approval
from platform_management.host_executor.inspection_probes import UID_LABEL

INSTANCE_LABEL = "klip.dev.node-restriction.kubernetes.io/provider-instance"
READY_LABEL = "klip.dev.node-restriction.kubernetes.io/gpu-ready"
WORKER_LABEL = "klip.dev.node-restriction.kubernetes.io/worker-class"
APPS = {
    "nvidia-driver-daemonset",
    "nvidia-container-toolkit-daemonset",
    "nvidia-device-plugin-daemonset",
    "gpu-feature-discovery",
    "nvidia-dcgm-exporter",
    "nvidia-operator-validator",
}
PRIMARY_IMAGES = {
    "nvidia-driver-daemonset": "nvcr.io/nvidia/driver:595.91.07-ubuntu24.04",
    "nvidia-container-toolkit-daemonset": "nvcr.io/nvidia/k8s/container-toolkit:v1.20.1",
    "nvidia-device-plugin-daemonset": "nvcr.io/nvidia/k8s-device-plugin:v0.20.1",
    "gpu-feature-discovery": "nvcr.io/nvidia/k8s-device-plugin:v0.20.1",
    "nvidia-dcgm-exporter": "nvcr.io/nvidia/k8s/dcgm-exporter:4.6.1-4.8.4-distroless",
    "nvidia-operator-validator": "nvcr.io/nvidia/gpu-operator:v26.7.2",
}


def progress(root, task, stage):
    path = root / "executions" / task.id / "observation.json"
    current = HostTaskObservation.model_validate_json(path.read_text())
    if current.task_id != task.id or current.status != "running":
        raise ValueError("Inspection lost its running task observation")
    write_private(
        path, {**current.model_dump(mode="json"), "stage": stage, "observed_at": utc_now()}
    )


def checked_node(task, node, *, require_gpu=False, promoted=False):
    assert_node_identity(task, node)
    labels = node["metadata"].get("labels", {})
    if (
        node["spec"].get("providerID") != "lambda:///" + task.provider_instance_id
        or node["spec"].get("unschedulable", False)
        or labels.get("klip.dev.node-restriction.kubernetes.io/platform")
        or not any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in node["status"].get("conditions", [])
        )
        or {"type": "InternalIP", "address": str(task.wireguard_ip)}
        not in node["status"].get("addresses", [])
    ):
        raise ValueError("Inspection reached an unready, cordoned or differently assigned worker")
    bootstrap = [t for t in node["spec"].get("taints", []) if t.get("key") == BOOTSTRAP["key"]]
    quarantined = any(
        t.get("key") == BOOTSTRAP["key"]
        and t.get("value") == "true"
        and t.get("effect") == "NoSchedule"
        for t in node["spec"].get("taints", [])
    )
    if quarantined == promoted or len(bootstrap) != (0 if promoted else 1):
        raise ValueError("GPU bootstrap quarantine differs from the inspection stage")
    if not promoted and labels.get(READY_LABEL) == "true":
        raise ValueError("An unverified bootstrap cannot already carry the GPU-ready label")
    for key, expected in {
        UID_LABEL: task.node_uid,
        INSTANCE_LABEL: task.provider_instance_id,
        WORKER_LABEL: "latency",
        **CANDIDATE,
    }.items():
        if key in labels and labels[key] != expected:
            raise ValueError("Previously admitted GPU node label belongs to another identity")
    if require_gpu and any(
        node["status"].get(field, {}).get("nvidia.com/gpu") != "1"
        for field in ("capacity", "allocatable")
    ):
        raise ValueError("Single-A10 inspection expected exactly one allocatable GPU")
    return node


def patch_node(cp, task, node, *, promote=False):
    checked_node(task, node, require_gpu=promote)
    labels = {**CANDIDATE, UID_LABEL: task.node_uid, INSTANCE_LABEL: task.provider_instance_id}
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": task.node_uid},
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": node["metadata"]["resourceVersion"],
        },
    ]
    if promote:
        labels.update({READY_LABEL: "true", WORKER_LABEL: "latency"})
        taints = node["spec"].get("taints", [])
        patch.extend(
            [
                {"op": "test", "path": "/spec/taints", "value": taints},
                {
                    "op": "replace",
                    "path": "/spec/taints",
                    "value": [
                        t
                        for t in taints
                        if not (
                            t.get("key") == BOOTSTRAP["key"]
                            and t.get("value") == "true"
                            and t.get("effect") == "NoSchedule"
                        )
                    ],
                },
            ]
        )
    for key, value in labels.items():
        patch.append(
            {
                "op": "add",
                "path": "/metadata/labels/" + key.replace("~", "~0").replace("/", "~1"),
                "value": value,
            }
        )
    observed = json.loads(
        kubectl(
            cp,
            "patch",
            "node",
            task.node_name,
            "--type=json",
            "-p",
            json.dumps(patch),
            "-o",
            "json",
        )
    )
    checked_node(task, observed, require_gpu=promote, promoted=promote)
    if any(observed["metadata"]["labels"].get(k) != v for k, v in labels.items()):
        raise ValueError("GPU node label patch was not observed")
    return observed


def operator_configuration(cp):
    desired = values()
    deployment = json.loads(
        kubectl(cp, "-n", "gpu-system", "get", "deployment", "gpu-operator", "-o", "json")
    )
    if (
        deployment["status"].get("observedGeneration") != deployment["metadata"]["generation"]
        or deployment["status"].get("availableReplicas") != 1
        or deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        != image("nvcr.io/nvidia/gpu-operator:v26.7.2")
    ):
        raise ValueError("GPU Operator deployment is stale or differs from its pinned image")
    policy = json.loads(kubectl(cp, "get", "clusterpolicy", "cluster-policy", "-o", "json"))
    spec = policy["spec"]
    for key in ("driver", "toolkit", "devicePlugin", "dcgmExporter", "gfd", "validator"):
        if (
            spec[key].get("enabled", True) is not True
            or spec[key]["version"] != desired[key]["version"]
        ):
            raise ValueError("Operator component is disabled or differs from its deployment pin")
        if spec[key]["repository"] + "/" + spec[key]["image"] + ":" + spec[key]["version"] not in {
            image(r) for r in settings()["images"]
        }:
            raise ValueError("Operator component repository differs from its immutable pin")
    if spec["driver"]["upgradePolicy"]["autoUpgrade"] or spec["mig"]["strategy"] != "none":
        raise ValueError("Automatic driver upgrades or partitioning are outside this experiment")
    if spec["dcgmExporter"]["serviceMonitor"]["additionalLabels"] != {"release": "klip-monitoring"}:
        raise ValueError("GPU metrics discovery has not been configured")
    for key in (
        "dcgm",
        "migManager",
        "gds",
        "gdrcopy",
        "vgpuManager",
        "vgpuDeviceManager",
        "vfioManager",
        "ccManager",
        "kataManager",
        "sandboxWorkloads",
        "nodeStatusExporter",
    ):
        if spec[key].get("enabled", False):
            raise ValueError("Unexpected optional GPU service is enabled")
    return {
        "deployment_uid": deployment["metadata"]["uid"],
        "policy_uid": policy["metadata"]["uid"],
        "policy_spec_sha256": hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
    }


def operands(cp, task):
    pods = json.loads(
        kubectl(
            cp,
            "-n",
            "gpu-system",
            "get",
            "pods",
            "--field-selector",
            "spec.nodeName=" + task.node_name,
            "-o",
            "json",
        )
    )["items"]
    allowed = {image(r) for r in settings()["images"]}
    result = []
    for app in sorted(APPS):
        selected = [
            p
            for p in pods
            if p["metadata"].get("labels", {}).get("app") == app
            and not p["metadata"].get("deletionTimestamp")
        ]
        if len(selected) != 1:
            return None
        pod = selected[0]
        if pod["spec"]["nodeName"] != task.node_name or not any(
            c["image"] == image(PRIMARY_IMAGES[app]) for c in pod["spec"]["containers"]
        ):
            raise ValueError("GPU operand has the wrong node or component image")
        if not any(
            o.get("kind") == "DaemonSet" and o.get("controller")
            for o in pod["metadata"].get("ownerReferences", [])
        ):
            raise ValueError("GPU operand is not owned by its native DaemonSet")
        statuses = pod["status"].get("containerStatuses", [])
        if not statuses or not all(s.get("ready") for s in statuses):
            return None
        statuses += pod["status"].get("initContainerStatuses", [])
        for container in pod["spec"].get("initContainers", []) + pod["spec"]["containers"]:
            if container["image"] not in allowed:
                raise ValueError("GPU operand runs an image outside the reviewed lock")
            observed = next((s for s in statuses if s["name"] == container["name"]), {})
            if not observed.get("imageID", "").endswith(container["image"].split("@")[1]):
                raise ValueError("GPU operand runtime image ID differs from its pinned manifest")
        result.append(
            {
                "app": app,
                "name": pod["metadata"]["name"],
                "uid": pod["metadata"]["uid"],
                "node": pod["spec"]["nodeName"],
                "images": [s["imageID"] for s in statuses],
                "containers": [
                    {
                        "name": s["name"],
                        "id": s.get("containerID"),
                        "restarts": s.get("restartCount", 0),
                    }
                    for s in statuses
                ],
            }
        )
    return result


def wireguard(task, registry, hosts):
    from cluster import Host

    gpu = Host(hosts[task.node_name])
    expected = {p.public_key for name, p in registry.peers.items() if name != task.node_name}
    receipts = []
    for name, peer in registry.peers.items():
        if name == task.node_name:
            continue
        # Both directions and a non-fragmenting WG1420 packet, not just TCP 6443.
        gpu.run(["ping", "-c", "1", "-W", "2", "-M", "do", "-s", "1392", str(peer.wireguard_ip)])
        Host(hosts[name]).run(
            ["ping", "-c", "1", "-W", "2", "-M", "do", "-s", "1392", str(task.wireguard_ip)]
        )
        receipts.append(name)
    observed = {
        line.split()[0]: int(line.split()[1])
        for line in gpu.run(["sudo", "wg", "show", "wg0", "latest-handshakes"]).splitlines()
    }
    if set(observed) != expected or any(
        not 0 <= time.time() - stamp <= 180 for stamp in observed.values()
    ):
        raise ValueError("GPU WireGuard peers are missing, unexpected or stale")
    return {"direct_peers": sorted(receipts), "mtu": 1420, "handshakes": observed}


def cilium(cp, task):
    pods = json.loads(
        kubectl(
            cp,
            "-n",
            "kube-system",
            "get",
            "pods",
            "-l",
            "k8s-app=cilium",
            "--field-selector",
            "spec.nodeName=" + task.node_name,
            "-o",
            "json",
        )
    )["items"]
    if (
        len(pods) != 1
        or not pods[0]["status"].get("containerStatuses")
        or not all(s.get("ready") for s in pods[0]["status"]["containerStatuses"])
    ):
        raise ValueError("The admitted node's Cilium agent is not Ready")
    state = kubectl(
        cp,
        "-n",
        "kube-system",
        "exec",
        pods[0]["metadata"]["name"],
        "-c",
        "cilium-agent",
        "--",
        "cilium-dbg",
        "status",
        "--brief",
    )
    return {"pod": pods[0]["metadata"]["name"], "uid": pods[0]["metadata"]["uid"], "status": state}


def perform_inspection(root, task, inputs, registry, cp):
    from cluster import Host, assert_peer_writer, load_hosts

    from platform_management.host_executor.gpu_telemetry import observe
    from platform_management.host_executor.inspection_probes import execute

    assert_peer_writer()
    approved = approval(root, inputs["kube_system_uid"])
    checked_peer(registry, task)
    hosts = load_hosts()
    fields = hosts.get(task.node_name, {})
    if fields.get("ansible_host") != str(task.public_ip) or any(
        fields.get("klip_node", {}).get(k) != value
        for k, value in {
            "instance_id": task.provider_instance_id,
            "wireguard_ip": str(task.wireguard_ip),
            "runtime_profile": "nvidia-operator",
            "provider": "lambda",
        }.items()
    ):
        raise ValueError("GPU inspection inventory differs from its admitted allocation")
    before_nodes = json.loads(kubectl(cp, "get", "nodes", "-o", "json"))["items"]
    node_uids = {n["metadata"]["name"]: n["metadata"]["uid"] for n in before_nodes}
    if (
        len(node_uids) != len(before_nodes)
        or not (set(inputs["cpu_peers"]) | {task.node_name}) <= node_uids.keys()
    ):
        raise ValueError("GPU inspection requires all six CPU nodes and its bound worker")
    for node in before_nodes:
        peer = registry.peers.get(node["metadata"]["name"])
        if peer is None or node["spec"].get("providerID", "").split("/")[-1] != peer.instance_id:
            raise ValueError(
                "Kubernetes node identity differs from the authoritative peer registry"
            )
    node = checked_node(
        task, next(n for n in before_nodes if n["metadata"]["name"] == task.node_name)
    )
    host = Host(fields)
    host_state = host.agent("preflight")
    if host_state != {"host": True, "instance_id": task.provider_instance_id}:
        raise ValueError("GPU host prerequisites are not verified")
    kernel = observe_kernel(host)
    operator = operator_configuration(cp)
    if approved["operator"] != operator:
        raise ValueError(
            "Live GPU Operator identity or configuration differs from runtime approval"
        )
    progress(root, task, "Checking direct WireGuard peers and effective host MTU")
    network = wireguard(task, registry, hosts)
    progress(root, task, "Configuring operator-managed GPU software on the admitted node")
    patch_node(cp, task, node)
    while datetime.now(UTC) < task.deadline:
        checked_node(task, json.loads(kubectl(cp, "get", "node", task.node_name, "-o", "json")))
        observed = operands(cp, task)
        if observed is not None:
            break
        time.sleep(2)
    else:
        raise ValueError("GPU Operator operands did not become ready before the task deadline")
    checked_node(
        task, json.loads(kubectl(cp, "get", "node", task.node_name, "-o", "json")), require_gpu=True
    )
    progress(root, task, "Validating CUDA, actual GPU memory and cross-host Pod networking")
    probes = execute(root, task, cp, inputs["cpu_peers"], host)
    progress(root, task, "Verifying fresh GPU telemetry on both Prometheus replicas")
    telemetry = observe(
        cp,
        task,
        probes["hardware"],
        next(p for p in observed if p["app"] == "nvidia-dcgm-exporter"),
    )
    cilium_state = cilium(cp, task)
    if operator_configuration(cp) != operator:
        raise ValueError("GPU Operator configuration changed during inspection")
    if operands(cp, task) != observed:
        raise ValueError("GPU operands changed or restarted during inspection")
    if observe_kernel(host) != kernel:
        raise ValueError("GPU host rebooted or its kernel prerequisites changed during inspection")
    after = json.loads(kubectl(cp, "get", "nodes", "-o", "json"))["items"]
    if {n["metadata"]["name"]: n["metadata"]["uid"] for n in after} != node_uids:
        raise ValueError("Cluster node identities changed during GPU/network inspection")
    if approval(root, inputs["kube_system_uid"]) != approved:
        raise ValueError("GPU runtime approval changed during inspection")
    # Every potentially failing workload/probe/telemetry step precedes promotion.
    capacity = GPUCapacityObservation(
        observed_at=utc_now(), devices=(probes["hardware"],), allocatable_gpu_count=1
    )
    evidence = {
        "task_id": task.id,
        "node_uid": task.node_uid,
        "runtime_approval": approved,
        "operator": operator,
        "kernel": kernel,
        "operands": observed,
        "wireguard": network,
        "probes": probes,
        "telemetry": telemetry,
        "cilium": cilium_state,
        "gpu_observation": capacity.model_dump(mode="json"),
        "observed_at": utc_now(),
    }
    # Persist the complete proof before permitting serving placement. If durable
    # storage is unavailable, the bootstrap remains quarantined.
    write_private(root / "executions" / task.id / "inspection-proof.json", evidence)
    final = patch_node(
        cp, task, next(n for n in after if n["metadata"]["name"] == task.node_name), promote=True
    )
    evidence["node"] = final
    write_private(root / "executions" / task.id / "inspection-evidence.json", evidence)
    return {
        "status": "completed",
        "provider_instance_id": task.provider_instance_id,
        "registry_revision": registry.revision,
        "node_name": task.node_name,
        "node_uid": task.node_uid,
        "readiness": dict.fromkeys(
            (
                "host",
                "wireguard",
                "kubernetes",
                "cilium",
                "gpu_runtime",
                "connectivity",
                "gpu_canary",
            ),
            True,
        ),
        "gpu_observation": capacity.model_dump(mode="json"),
        "evidence_sha256": hashlib.sha256(
            json.dumps(evidence, sort_keys=True).encode()
        ).hexdigest(),
    }
