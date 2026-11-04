"""Install read-only local host observations on six identity-bound CPU hosts."""

from __future__ import annotations

import fcntl
import json

import yaml
from common import CONFIG, ROOT, private_dir, source_digest, state_home, utc_now, write_private


def deploy():
    from cluster import Host, ansible, kubectl, load_hosts
    from database import verify
    from platform_resize import NODES

    hosts = load_hosts()
    expected = set(CONFIG["nodes"]) | NODES
    nodes = {
        n["metadata"]["name"]: n for n in json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    }
    folder = private_dir(state_home() / "cluster/observability/host-agents" / source_digest())
    path = folder / "operation.json"
    record = (
        json.loads(path.read_text())
        if path.exists()
        else {"started_at": utc_now(), "source_sha256": source_digest(), "nodes": {}}
    )
    verify()
    for name in sorted(expected):
        fields = hosts[name]
        host = Host(fields)
        config = json.loads(host.run(["sudo", "cat", "/etc/klip/node.json"]))
        node = nodes[name]
        if (
            config["name"] != name
            or config["provider"] != "aws"
            or config["instance_id"] != fields["klip_node"]["instance_id"]
            or not node["spec"].get("providerID", "").endswith("/" + config["instance_id"])
        ):
            raise ValueError("Host identity differs before local telemetry installation")
        inventory = folder / (name + ".yaml")
        write_private(
            inventory,
            yaml.safe_dump(
                {
                    "all": {
                        "vars": {"klip_repository": str(ROOT)},
                        "hosts": {name: {**fields, "klip_node": config}},
                    }
                }
            ),
        )
        record["nodes"][name] = {
            "uid": node["metadata"]["uid"],
            "instance_id": config["instance_id"],
            "stage": "installing",
        }
        write_private(path, record)
        ansible("host-telemetry", inventory=inventory, log_path=folder / (name + ".ansible.log"))
        actual = json.loads(kubectl("get", "node", name, "-o", "json"))
        text = host.run(["sudo", "cat", "/var/lib/node_exporter/klip/wireguard.prom"])
        if (
            actual["metadata"]["uid"] != node["metadata"]["uid"]
            or "\nklip_wireguard_observation_success 1\n" not in text
            or "klip_wireguard_unknown_peers 0" not in text
        ):
            raise ValueError("Node identity or public WireGuard observation failed")
        health = Host(hosts["cp-01"]).agent("etcd")
        if len(health["health"]) != 3 or not all(item["health"] for item in health["health"]):
            raise ValueError("Stop telemetry rollout: original etcd quorum is not healthy")
        verify()
        write_private(folder / (name + ".prom"), text)
        record["nodes"][name].update(stage="completed", completed_at=utc_now())
        write_private(path, record)
    record["completed_at"] = utc_now()
    write_private(path, record)
    return record


if __name__ == "__main__":
    with (private_dir(state_home() / "cluster") / "operator.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(json.dumps(deploy(), indent=2))
