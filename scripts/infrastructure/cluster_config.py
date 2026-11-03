"""Deterministic host, firewall and kubeadm configuration generation."""

from __future__ import annotations

from pathlib import Path

import yaml
from common import CONFIG, ROOT, state_home, write_private
from platform_artifacts import VERSIONS


def node_configuration(
    name: str, allocation: dict, node: dict, *, role: str = "control_plane", provider: str = "aws"
) -> dict:
    return {
        "name": name,
        "role": role,
        "provider": provider,
        "instance_id": node["instance_id"],
        "account_id": CONFIG["account_id"],
        "region": CONFIG["region"],
        "private_ip": node["private_ip"],
        "wireguard_ip": node["wireguard_ip"],
        "etcd_volume_id": node.get("etcd_volume_id"),
        "availability_zone": node.get("availability_zone", "external"),
        "api_endpoint": allocation["api"]["endpoint"].rstrip("/"),
        **{
            key: allocation["network"][key]
            for key in (
                "vpc_cidr",
                "wireguard_cidr",
                "pod_cidr",
                "service_cidr",
                "api_routed_cidrs",
            )
        },
        "api_frontend_ips": allocation["api"]["frontend_ips"],
        "native_ips": [node["private_ip"] for node in allocation["nodes"].values()],
        "kubernetes_version": VERSIONS["kubernetes"],
        "containerd_version": VERSIONS["containerd"],
        "runc_version": VERSIONS["runc"],
        "backup_bucket": f"klip-backups-{CONFIG['account_id']}-{CONFIG['region']}",
    }


def firewall(config: dict) -> str:
    rules = [
        "*filter",
        ":KLIP-HOST - [0:0]",
        ":KLIP-FORWARD - [0:0]",
        "-F KLIP-HOST",
        "-F KLIP-FORWARD",
        "-A KLIP-HOST -i lo -j ACCEPT",
        "-A KLIP-HOST -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        "-A KLIP-HOST ! -i wg0 -p udp --sport 67 --dport 68 -j ACCEPT",
        "-A KLIP-HOST -p udp --dport 51820 -j ACCEPT",
        "-A KLIP-HOST -p icmp --icmp-type 3/4 -j ACCEPT",
    ]
    for ip in config["api_frontend_ips"]:
        rules.append(f"-A KLIP-HOST -s {ip}/32 -p tcp --dport 6443 -j ACCEPT")
    if config["provider"] == "lambda" and config["role"] == "worker":
        for source in config.get("ssh_source_cidrs", []):
            rules.append(f"-A KLIP-HOST -s {source} -p tcp --dport 22 -j ACCEPT")
    for ip in ("10.200.0.11", "10.200.0.12", "10.200.0.13"):
        rules.append(
            f"-A KLIP-HOST -i wg0 -s {ip}/32 -p tcp "
            "-m multiport --dports 2379,2380,10250,4244 -j ACCEPT"
        )
    rules += [
        f"-A KLIP-HOST -i wg0 -s {config['wireguard_cidr']} -p tcp --dport 6443 -j ACCEPT",
        f"-A KLIP-HOST -i wg0 -s {config['wireguard_cidr']} -p udp --dport 8472 -j ACCEPT",
        f"-A KLIP-HOST -i wg0 -s {config['wireguard_cidr']} -p tcp --dport 4240 -j ACCEPT",
        f"-A KLIP-HOST -i wg0 -s {config['wireguard_cidr']} -p icmp -j ACCEPT",
        f"-A KLIP-HOST -s {config['pod_cidr']} -p tcp "
        "-m multiport --dports 6443,10250,4240,4244 -j ACCEPT",
        f"-A KLIP-HOST -s {config['pod_cidr']} -p icmp -j ACCEPT",
        "-A KLIP-HOST -j DROP",
        "-A KLIP-FORWARD -d 169.254.169.254/32 -j DROP",
    ]
    for cidr in config["api_routed_cidrs"]:
        rules += [
            f"-A KLIP-FORWARD -i wg0 -s {config['wireguard_cidr']} -d {cidr} "
            "-p tcp --dport 6443 -j ACCEPT"
        ]
    rules += [
        f"-A KLIP-FORWARD -i wg0 -d {config['vpc_cidr']} -j DROP",
        "-A KLIP-FORWARD -j RETURN",
        "COMMIT",
        "*nat",
        ":KLIP-API-SNAT - [0:0]",
        "-F KLIP-API-SNAT",
    ]
    if config["role"] == "control_plane":
        rules += [
            f"-A KLIP-API-SNAT -s {config['wireguard_cidr']} -d {cidr} "
            f"-p tcp --dport 6443 -j SNAT --to-source {config['private_ip']}"
            for cidr in config["api_routed_cidrs"]
        ]
    return "\n".join(rules + ["COMMIT", ""])


def kubeadm_documents(
    config: dict, *, join: dict | None = None, control_plane: bool = True
) -> list[dict]:
    node = {
        "name": config["name"],
        "criSocket": "unix:///run/containerd/containerd.sock",
        "kubeletExtraArgs": [
            {"name": "node-ip", "value": config["wireguard_ip"]},
            {
                "name": "provider-id",
                "value": (
                    f"aws:///{config['availability_zone']}/{config['instance_id']}"
                    if config["provider"] == "aws"
                    else f"lambda:///{config['instance_id']}"
                ),
            },
        ],
    }
    local = {"advertiseAddress": config["wireguard_ip"], "bindPort": 6443}
    if join:
        if config.get("quarantine"):
            node["taints"] = [
                {"key": "klip.dev/bootstrap", "value": "true", "effect": "NoSchedule"}
            ]
        if not control_plane and config["name"] == "network-probe":
            node["taints"] = [
                {"key": "klip.io/acceptance-only", "value": "true", "effect": "NoSchedule"}
            ]
        document = {
            "apiVersion": "kubeadm.k8s.io/v1beta4",
            "kind": "JoinConfiguration",
            "nodeRegistration": node,
            "discovery": {"bootstrapToken": join},
        }
        if control_plane:
            document["controlPlane"] = {"localAPIEndpoint": local}
            document["patches"] = {"directory": "/etc/klip/patches"}
        return [document]
    return [
        {
            "apiVersion": "kubeadm.k8s.io/v1beta4",
            "kind": "InitConfiguration",
            "bootstrapTokens": [{"ttl": "15m", "description": "klip initialization"}],
            "localAPIEndpoint": local,
            "nodeRegistration": node,
            "patches": {"directory": "/etc/klip/patches"},
        },
        {
            "apiVersion": "kubeadm.k8s.io/v1beta4",
            "kind": "ClusterConfiguration",
            "clusterName": "klip",
            "kubernetesVersion": "v" + VERSIONS["kubernetes"],
            "controlPlaneEndpoint": config["api_endpoint"].removeprefix("https://"),
            "networking": {
                "podSubnet": config["pod_cidr"],
                "serviceSubnet": config["service_cidr"],
            },
            "apiServer": {
                "certSANs": [
                    config["api_endpoint"].split("://")[1].split(":")[0],
                    *config["api_frontend_ips"],
                    *config["native_ips"],
                    "10.200.0.11",
                    "10.200.0.12",
                    "10.200.0.13",
                ],
                "extraArgs": [
                    {"name": "encryption-provider-config", "value": "/etc/klip/encryption.yaml"}
                ],
                "extraVolumes": [
                    {
                        "name": "klip-encryption",
                        "hostPath": "/etc/klip/encryption.yaml",
                        "mountPath": "/etc/klip/encryption.yaml",
                        "readOnly": True,
                        "pathType": "File",
                    }
                ],
            },
            "etcd": {"local": {"dataDir": "/var/lib/etcd/data"}},
        },
        {
            "apiVersion": "kubelet.config.k8s.io/v1beta1",
            "kind": "KubeletConfiguration",
            "cgroupDriver": "systemd",
            "serverTLSBootstrap": True,
            "rotateCertificates": True,
            "systemReserved": {"cpu": "200m", "memory": "512Mi"},
            "kubeReserved": {"cpu": "300m", "memory": "768Mi"},
            "evictionHard": {
                "memory.available": "500Mi",
                "nodefs.available": "10%",
                "imagefs.available": "15%",
            },
            "containerLogMaxSize": "10Mi",
            "containerLogMaxFiles": 3,
            "maxPods": 64,
        },
    ]


def write_ansible_inputs(
    observation: dict, inventory: dict, recipient: str | None = None, role_id: str | None = None
) -> Path:
    allocation = observation["allocation"]
    for name, host in inventory["all"]["children"]["control_plane"]["hosts"].items():
        config = node_configuration(name, allocation, allocation["nodes"][name])
        if recipient:
            config["backup_recipient"] = recipient
        if role_id:
            config["backup_principal_id"] = role_id + ":" + config["instance_id"]
        host["klip_node"] = config
        path = state_home() / f"cluster/firewall-{name}.rules"
        write_private(path, firewall(config))
        host["klip_firewall_file"] = str(path)
    inventory["all"]["vars"] = {
        "klip_repository": str(ROOT),
        "klip_peer_registry": str(state_home() / "cluster/peers.json"),
        "klip_ca_bundle": str(state_home() / "cluster/ca.crt"),
    }
    path = state_home() / "cluster/inventory.yaml"
    write_private(path, yaml.safe_dump(inventory, sort_keys=False))
    return path
