"""Live checks: allocations never imply network or Kubernetes readiness."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

from common import CONFIG, PROFILES, Aws, state_home, utc_now
from platform_contracts.inventory import NodeObservation


def ready_node(node: dict, provider_id: str, address: str) -> None:
    if not node["spec"].get("providerID", "").endswith("/" + provider_id):
        raise ValueError("Observed Kubernetes provider identity differs from allocation")
    if not any(
        condition["type"] == "Ready" and condition["status"] == "True"
        for condition in node["status"]["conditions"]
    ):
        raise ValueError("Kubernetes node is not Ready")
    if not any(
        item == {"type": "InternalIP", "address": address} for item in node["status"]["addresses"]
    ):
        raise ValueError("Node InternalIP is not its WireGuard assignment")


def foundation_nodes(observed: list[dict], hosts: dict) -> dict:
    """Keep the CPU-foundation proof scoped to etcd hosts when known workers are present."""
    nodes = {item["metadata"]["name"]: item for item in observed}
    expected = set(CONFIG["nodes"])
    if len(nodes) != len(observed) or not expected.issubset(nodes):
        raise ValueError("Missing or duplicate foundation Kubernetes nodes")
    for name in set(nodes) - expected:
        config = hosts.get(name, {}).get("klip_node", {})
        if (
            config.get("role") != "worker"
            or nodes[name]["spec"].get("providerID", "").split("/")[-1] != config.get("instance_id")
            or "node-role.kubernetes.io/control-plane" in nodes[name]["metadata"].get("labels", {})
        ):
            raise ValueError("Unknown or incompatible worker in the foundation observation")
    return {name: nodes[name] for name in sorted(expected)}


def verify_wireguard(hosts: dict) -> None:
    from cluster import Host

    cpu = {
        name: fields
        for name, fields in hosts.items()
        if fields["klip_node"]["role"] == "control_plane"
    }
    for name, fields in cpu.items():
        host = Host(fields)
        for other, peer in cpu.items():
            if other != name:
                host.run(
                    [
                        "ping",
                        "-c",
                        "2",
                        "-W",
                        "2",
                        "-M",
                        "do",
                        "-s",
                        "1392",
                        peer["klip_node"]["wireguard_ip"],
                    ]
                )
        handshakes = host.run(["sudo", "wg", "show", "wg0", "latest-handshakes"])
        times = [int(line.split()[1]) for line in handshakes.splitlines()]
        if len(times) < 2 or any(time.time() - stamp > 180 for stamp in times):
            raise ValueError("WireGuard peer handshake is missing or stale")


def verify_cluster(hosts: dict, kubectl) -> tuple[dict, list[dict]]:
    from cluster import Host

    verify_wireguard(hosts)
    allocation = json.loads((state_home() / "reports/foundation.json").read_text())["allocation"]
    targets = Aws(PROFILES["foundation"]).call(
        "elbv2",
        "describe-target-health",
        "--target-group-arn",
        allocation["api"]["target_group_arn"],
    )["TargetHealthDescriptions"]
    if {item["Target"]["Id"] for item in targets} != {
        node["private_ip"] for node in allocation["nodes"].values()
    } or not all(item["TargetHealth"]["State"] == "healthy" for item in targets):
        raise ValueError("All three API NLB backends must be healthy")
    observed = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    nodes = foundation_nodes(observed, hosts)
    cilium = json.loads(
        kubectl("-n", "kube-system", "get", "pods", "-l", "k8s-app=cilium", "-o", "json")
    )["items"]
    cilium_ready = {
        pod["spec"]["nodeName"]
        for pod in cilium
        if all(item.get("ready") for item in pod["status"].get("containerStatuses", []))
        and pod["status"].get("containerStatuses")
    }
    if not set(nodes).issubset(cilium_ready):
        raise ValueError("Cilium is not ready on every CPU node")
    health = Host(hosts["cp-01"]).agent("etcd")
    if (
        len(health["members"]) != 3
        or len(health["health"]) != 3
        or not all(item["health"] for item in health["health"])
    ):
        raise ValueError("Three healthy etcd members are required")
    results = []
    for name in sorted(nodes):
        fields = hosts[name]
        config = fields["klip_node"]
        ready_node(nodes[name], config["instance_id"], config["wireguard_ip"])
        if not any(
            taint["key"] == "node-role.kubernetes.io/control-plane"
            and taint["effect"] == "NoSchedule"
            for taint in nodes[name]["spec"].get("taints", [])
        ):
            raise ValueError("Control-plane scheduling taint is missing")
        Host(fields).agent("preflight")
        Host(fields).agent("pod-network")
        pod = next(pod for pod in cilium if pod["spec"]["nodeName"] == name)
        kubectl(
            "-n",
            "kube-system",
            "exec",
            pod["metadata"]["name"],
            "-c",
            "cilium-agent",
            "--",
            "cilium-dbg",
            "status",
            "--brief",
        )
        # Probe a three-replica Service and DNS from each node's actual Pod network.
        smoke_pods = json.loads(
            kubectl("-n", "klip-acceptance", "get", "pods", "-l", "app=smoke", "-o", "json")
        )["items"]
        smoke = next(
            pod
            for pod in smoke_pods
            if pod["spec"]["nodeName"] == name and pod["status"].get("phase") == "Running"
        )
        kubectl(
            "-n",
            "klip-acceptance",
            "exec",
            smoke["metadata"]["name"],
            "--",
            "/agnhost",
            "connect",
            "--protocol=tcp",
            "--timeout=5s",
            "smoke.klip-acceptance.svc.cluster.local:8080",
        )
        for target in smoke_pods:
            kubectl(
                "-n",
                "klip-acceptance",
                "exec",
                smoke["metadata"]["name"],
                "--",
                "/agnhost",
                "connect",
                "--protocol=tcp",
                "--timeout=5s",
                target["status"]["podIP"] + ":8080",
            )
        try:
            kubectl(
                "-n",
                "klip-acceptance",
                "exec",
                smoke["metadata"]["name"],
                "--",
                "/agnhost",
                "connect",
                "--protocol=tcp",
                "--timeout=2s",
                "169.254.169.254:80",
            )
        except RuntimeError:
            pass
        else:
            raise ValueError("A workload Pod can reach instance metadata")
        item = NodeObservation.model_validate(
            {
                "id": name,
                "provider": "aws",
                "provider_instance_id": config["instance_id"],
                "managed_by": "foundation",
                "role": "control_plane",
                "public_ip": allocation["nodes"][name]["public_ip"],
                "wireguard_ip": config["wireguard_ip"],
                "observed_at": utc_now(),
                "access": {
                    "ssh_user": fields["ansible_user"],
                    "ssh_credential_ref": CONFIG["ssh_credential_ref"],
                },
                "readiness": {
                    "host": True,
                    "wireguard": True,
                    "kubernetes": True,
                    "cilium": True,
                    "gpu_runtime": False,
                },
                "kubernetes": {
                    "name": name,
                    "uid": nodes[name]["metadata"]["uid"],
                    "allocatable_gpu_count": 0,
                    "allocated_gpu_count": 0,
                },
            }
        )
        results.append(item.model_dump(mode="json"))
    first = next(pod for pod in smoke_pods if pod["spec"]["nodeName"] == "cp-01")
    second = next(pod for pod in smoke_pods if pod["spec"]["nodeName"] == "cp-02")
    with ThreadPoolExecutor(max_workers=1) as executor:
        capture = executor.submit(
            Host(hosts["cp-01"]).run,
            [
                "sudo",
                "timeout",
                "15",
                "tcpdump",
                "-nn",
                "-i",
                "wg0",
                "-c",
                "1",
                "udp",
                "port",
                "8472",
            ],
            timeout=25,
        )
        time.sleep(2)
        kubectl(
            "-n",
            "klip-acceptance",
            "exec",
            first["metadata"]["name"],
            "--",
            "/agnhost",
            "connect",
            "--protocol=tcp",
            "--timeout=5s",
            second["status"]["podIP"] + ":8080",
        )
        packet = capture.result()
        if "10.200.0." not in packet or "8472" not in packet:
            raise ValueError("VXLAN traffic was not observed on the WireGuard interface")
    return {
        "schema_version": "klip-cluster-verification.v1",
        "captured_at": utc_now(),
        "nodes": list(nodes),
        "etcd_members": 3,
        "network_checks": True,
    }, results
