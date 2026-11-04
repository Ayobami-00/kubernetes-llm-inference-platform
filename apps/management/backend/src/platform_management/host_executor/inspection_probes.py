"""Bounded native Pods prove CUDA and actual Pod/DNS/Service connectivity.

No probe owns a retrying controller. Every resource has a deterministic task
identity, frozen input hash, deadline and UID-guarded cleanup.
"""

import csv
import hashlib
import ipaddress
import json
from datetime import UTC, datetime

from common import write_private
from gpu_operator import BOOTSTRAP, CANDIDATE, GPU_TOLERATION, image

from platform_management.host_executor.cleanup import kubectl
from platform_management.integrations.job_runner import frozen_file

NETWORK_IMAGE = (
    "registry.k8s.io/e2e-test-images/agnhost:2.53@sha256:"
    "99c6b4bb4a1e1df3f0b3752168c89358794d02258ebebc26bf21c29399011a85"
)
UID_LABEL = "klip.dev.node-restriction.kubernetes.io/gpu-node-uid"
TASK_LABEL = "klip.dev/host-task"
HASH_ANNOTATION = "klip.dev/probe-manifest-sha256"
NAMESPACE = "klip-acceptance"


def identity(task, suffix=None):
    return "smoke-" + task.id + ("-" + suffix if suffix else "")


def frozen_manifest(task, document):
    document["metadata"].setdefault("labels", {})[TASK_LABEL] = task.id
    encoded = json.dumps(document, sort_keys=True).encode()
    document["metadata"].setdefault("annotations", {})[HASH_ANNOTATION] = hashlib.sha256(
        encoded
    ).hexdigest()
    return document


def pod(task, suffix, node, *, cuda=False):
    seconds = min(300, int((task.deadline - datetime.now(UTC)).total_seconds()) - 15)
    if seconds < 30:
        raise ValueError("Insufficient remaining time for bounded host probes and cleanup")
    security = {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    container = {
        "name": "probe",
        "image": NETWORK_IMAGE,
        "args": ["netexec", "--http-port=8080"],
        "resources": {
            "requests": {"cpu": "10m", "memory": "32Mi"},
            "limits": {"cpu": "100m", "memory": "64Mi"},
        },
        "securityContext": security,
        "readinessProbe": {"httpGet": {"path": "/hostname", "port": 8080}, "periodSeconds": 2},
    }
    spec = {
        "automountServiceAccountToken": False,
        "restartPolicy": "Never",
        "activeDeadlineSeconds": seconds,
        "terminationGracePeriodSeconds": 5,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10001,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "tolerations": [
            BOOTSTRAP,
            GPU_TOLERATION,
            {
                "key": "node-role.kubernetes.io/control-plane",
                "operator": "Exists",
                "effect": "NoSchedule",
            },
        ],
        "containers": [container],
    }
    if node == task.node_name:
        spec["nodeSelector"] = {
            **CANDIDATE,
            UID_LABEL: task.node_uid,
            "kubernetes.io/hostname": node,
        }
    else:
        # Only the six identity-checked permanent hosts are provided by the caller.
        spec["nodeName"] = node
    if cuda:
        spec["runtimeClassName"] = "nvidia"
        container.pop("args")
        container.pop("readinessProbe")
        container["image"] = image("nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda12.5.0")
        container["resources"] = {
            "requests": {"cpu": "100m", "memory": "128Mi", "nvidia.com/gpu": "1"},
            "limits": {"cpu": "1", "memory": "512Mi", "nvidia.com/gpu": "1"},
        }
        spec["initContainers"] = [
            {
                **container,
                "name": "hardware",
                "command": ["nvidia-smi"],
                "args": [
                    "--query-gpu=index,uuid,name,memory.total,driver_version",
                    "--format=csv,noheader,nounits",
                ],
            }
        ]
        # Init and main run sequentially: the Pod reserves one GPU, not two.
    return frozen_manifest(
        task,
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": identity(task, suffix),
                "namespace": "gpu-system" if cuda else NAMESPACE,
                "labels": {"klip.dev/probe-target": suffix},
            },
            "spec": spec,
        },
    )


def manifests(task, cpu_names):
    if set(cpu_names) != {"cp-01", "cp-02", "cp-03", "platform-01", "platform-02", "platform-03"}:
        raise ValueError("Network probes require exactly the protected six CPU hosts")
    documents = [pod(task, n, n) for n in sorted(cpu_names)]
    documents.append(pod(task, "gpu", task.node_name))
    documents.append(pod(task, "cuda", task.node_name, cuda=True))
    documents.append(
        frozen_manifest(
            task,
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": identity(task), "namespace": NAMESPACE},
                "spec": {
                    "selector": {TASK_LABEL: task.id, "klip.dev/probe-target": "gpu"},
                    "ports": [{"port": 8080, "targetPort": 8080}],
                },
            },
        )
    )
    return documents


def owned_probe(expected, observed):
    a, b = expected["metadata"], observed["metadata"]
    if (
        a["name"] != b["name"]
        or a["namespace"] != b["namespace"]
        or a["annotations"][HASH_ANNOTATION] != b.get("annotations", {}).get(HASH_ANNOTATION)
        or a["labels"][TASK_LABEL] != b.get("labels", {}).get(TASK_LABEL)
    ):
        raise ValueError("Probe name was occupied by a foreign or changed resource")
    if expected["kind"] == "Pod":
        for field in ("containers", "initContainers"):
            wanted, actual = expected["spec"].get(field, []), observed["spec"].get(field, [])
            if len(wanted) != len(actual) or any(
                any(a.get(k) != b.get(k) for k in ("name", "image", "command", "args"))
                for a, b in zip(wanted, actual, strict=True)
            ):
                raise ValueError("Probe container image or executable changed after admission")
        for field in (
            "nodeSelector",
            "automountServiceAccountToken",
            "restartPolicy",
            "securityContext",
        ):
            if observed["spec"].get(field) != expected["spec"].get(field):
                raise ValueError("Probe security or placement changed after admission")
    return b["uid"]


def cleanup(cp, documents):
    from cluster import delete_owned_resource

    removed = []
    for expected in reversed(documents):
        kind = "pods" if expected["kind"] == "Pod" else "services"
        metadata = expected["metadata"]
        raw = kubectl(
            cp,
            "-n",
            metadata["namespace"],
            "get",
            kind,
            metadata["name"],
            "--ignore-not-found",
            "-o",
            "json",
        )
        if raw.strip():
            uid = owned_probe(expected, json.loads(raw))
            delete_owned_resource(
                f"/api/v1/namespaces/{metadata['namespace']}/{kind}/{metadata['name']}",
                uid,
                host=cp,
            )
            removed.append(
                {"namespace": metadata["namespace"], "name": metadata["name"], "uid": uid}
            )
    return removed


def cleanup_chain(root, task, cp):
    """A fresh explicit removal also cleans probes left by a lost native owner."""
    from platform_contracts import HostTask

    identity = task.previous_task_id
    seen = set()
    while identity:
        if identity in seen or len(seen) >= 64:
            raise ValueError("Host probe cleanup chain is cyclic or exceeds its bound")
        seen.add(identity)
        directory = root / "executions" / identity
        previous = HostTask.model_validate_json((directory / "task.json").read_text())
        if (
            previous.id != identity
            or any(
                getattr(previous, k) != getattr(task, k)
                for k in ("provider_instance_id", "node_name", "public_ip", "wireguard_ip")
            )
            or (previous.node_uid is not None and previous.node_uid != task.node_uid)
        ):
            raise ValueError("Probe cleanup cannot cross another provider or node identity")
        path = directory / "probe-manifests.json"
        if path.exists():
            if path.is_symlink() or path.stat().st_size > 256 * 1024:
                raise ValueError("Invalid frozen probe manifest")
            documents = json.loads(path.read_text())
            if len(documents) != 9 or any(
                d["metadata"]["labels"].get(TASK_LABEL) != previous.id for d in documents
            ):
                raise ValueError("Probe manifest no longer belongs to its frozen task")
            for document in documents:
                metadata = document["metadata"]
                expected_hash = metadata["annotations"][HASH_ANNOTATION]
                unsigned = json.loads(json.dumps(document))
                unsigned["metadata"]["annotations"].pop(HASH_ANNOTATION)
                if not unsigned["metadata"]["annotations"]:
                    unsigned["metadata"].pop("annotations")
                if (
                    hashlib.sha256(json.dumps(unsigned, sort_keys=True).encode()).hexdigest()
                    != expected_hash
                ):
                    raise ValueError("Frozen probe input hash differs during cleanup")
            cleanup(cp, documents)
        identity = previous.previous_task_id


def hardware(text, *, expected_driver="595.91.07"):
    from platform_contracts.inventory import GPUDeviceObservation

    rows = list(csv.reader(text.strip().splitlines(), skipinitialspace=True))
    if len(rows) != 1 or len(rows[0]) != 5:
        raise ValueError("Single-A10 inspection expected exactly one NVIDIA GPU")
    index, uuid, name, memory, driver = (v.strip() for v in rows[0])
    value = GPUDeviceObservation(
        index=int(index), uuid=uuid, name=name, memory_total_mib=int(memory), driver_version=driver
    )
    if (
        value.index != 0
        or value.name not in {"NVIDIA A10", "A10"}
        or not 22000 <= value.memory_total_mib <= 25000
        or expected_driver not in {"595.91.07", "580.105.08"}
        or value.driver_version != expected_driver
    ):
        raise ValueError(
            "Observed GPU hardware, usable memory or driver differs from the A10 profile"
        )
    return value


def execute(root, task, cp, cpu_names, gpu_host, *, expected_driver="595.91.07"):
    directory = root / "executions" / task.id
    documents = manifests(task, cpu_names)
    frozen_file(directory / "probe-manifests.json", json.dumps(documents, sort_keys=True).encode())
    evidence = {"pods": [], "connections": [], "cleanup": []}
    try:
        for document in documents:
            observed = json.loads(
                cp.run(
                    [
                        "sudo",
                        "kubectl",
                        "--kubeconfig=/etc/kubernetes/admin.conf",
                        "create",
                        "-f",
                        "-",
                        "-o",
                        "json",
                    ],
                    data=json.dumps(document),
                )
            )
            owned_probe(document, observed)
        # Waits share the enclosing task's absolute deadline; never 8 separate clocks.
        for document in documents:
            if document["kind"] != "Pod":
                continue
            metadata = document["metadata"]
            cuda = metadata["namespace"] == "gpu-system"
            condition = "jsonpath={.status.phase}=Succeeded" if cuda else "condition=Ready"
            kubectl(
                cp,
                "-n",
                metadata["namespace"],
                "wait",
                "--for=" + condition,
                "pod/" + metadata["name"],
                "--timeout=300s",
            )
            observed = json.loads(
                kubectl(
                    cp, "-n", metadata["namespace"], "get", "pod", metadata["name"], "-o", "json"
                )
            )
            uid = owned_probe(document, observed)
            if cuda:
                statuses = observed["status"].get("initContainerStatuses", []) + observed[
                    "status"
                ].get("containerStatuses", [])
                if len(statuses) != 2 or any(
                    s.get("state", {}).get("terminated", {}).get("exitCode") != 0 for s in statuses
                ):
                    raise ValueError("CUDA/hardware validation did not exit successfully")
                if any(
                    not s.get("imageID", "").endswith(
                        document["spec"]["containers"][0]["image"].split("@")[1]
                    )
                    for s in statuses
                ):
                    raise ValueError("CUDA runtime image ID differs from its immutable input")
                output = kubectl(cp, "-n", "gpu-system", "logs", metadata["name"], "-c", "probe")
                write_private(directory / "cuda-output.txt", output)
                if "Test PASSED" not in output:
                    raise ValueError("CUDA vector addition did not report its success marker")
                evidence["hardware"] = hardware(
                    kubectl(cp, "-n", "gpu-system", "logs", metadata["name"], "-c", "hardware"),
                    expected_driver=expected_driver,
                ).model_dump(mode="json")
            else:
                address = ipaddress.ip_address(observed["status"]["podIP"])
                expected_node = (
                    task.node_name
                    if metadata["labels"]["klip.dev/probe-target"] == "gpu"
                    else metadata["labels"]["klip.dev/probe-target"]
                )
                if (
                    address not in ipaddress.ip_network("10.244.0.0/16")
                    or observed["spec"]["nodeName"] != expected_node
                ):
                    raise ValueError("Network probe reached the wrong node or Pod CIDR")
                evidence["pods"].append(
                    {
                        "name": metadata["name"],
                        "uid": uid,
                        "node": expected_node,
                        "ip": str(address),
                    }
                )
        if len({p["ip"] for p in evidence["pods"]}) != 7:
            raise ValueError("Network proof requires seven distinct Pod addresses")
        for source in evidence["pods"]:
            targets = [p["ip"] + ":8080" for p in evidence["pods"] if p is not source]
            targets.append(identity(task) + "." + NAMESPACE + ".svc.cluster.local:8080")
            for target in targets:
                kubectl(
                    cp,
                    "-n",
                    NAMESPACE,
                    "exec",
                    source["name"],
                    "--",
                    "/agnhost",
                    "connect",
                    "--protocol=tcp",
                    "--timeout=5s",
                    target,
                )
                evidence["connections"].append({"source": source["name"], "target": target})
        gpu_pod = next(p for p in evidence["pods"] if p["node"] == task.node_name)
        evidence["mtu"] = gpu_host.agent("pod-network", pod_uid=gpu_pod["uid"])
        write_private(directory / "probe-evidence.json", evidence)
    finally:
        # A lost owner never restarts these probes. A later explicit removal also
        # reads the frozen manifest chain and cleans any resources left by loss.
        evidence["cleanup"] = cleanup(cp, documents)
        write_private(directory / "probe-evidence.json", evidence)
    return evidence
