"""Add only the three protected CPU peers' authenticated kubelet metrics path."""

from __future__ import annotations

import fcntl
import hashlib
import json

import yaml
from cluster_config import firewall, platform_metrics_sources
from common import CONFIG, private_dir, source_digest, state_home, utc_now, write_private


def reviewed_rules(config, before):
    desired = firewall({**config, "platform_metrics_sources": platform_metrics_sources()})
    if before.strip() == desired.strip():
        return desired
    baseline = firewall({**config, "platform_metrics_sources": []})
    expected = {
        f"-A KLIP-HOST -i wg0 -s {source} -p tcp --dport 10250 -j ACCEPT"
        for source in platform_metrics_sources()
    }
    if (
        before.strip() != baseline.strip()
        or set(desired.splitlines()) - set(baseline.splitlines()) != expected
        or set(baseline.splitlines()) - set(desired.splitlines())
    ):
        raise ValueError("Refuse an unrelated or drifted host firewall change")
    return desired


def deploy():
    from cluster import Host, ansible, kubectl, load_hosts
    from image_credentials import healthy_targets
    from platform_resize import NODES

    hosts = load_hosts()
    expected = set(CONFIG["nodes"]) | NODES
    if not expected <= set(hosts):
        raise ValueError("All six trusted CPU host identities are required")
    targets = healthy_targets()
    nodes = {
        n["metadata"]["name"]: n for n in json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    }
    source = source_digest()
    root = private_dir(state_home() / "cluster/observability/firewall" / source)
    record_path = root / "operation.json"
    record = (
        json.loads(record_path.read_text())
        if record_path.exists()
        else {"started_at": utc_now(), "source_sha256": source, "nodes": {}}
    )
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
            or (name in targets and node["metadata"]["uid"] != targets[name]["uid"])
        ):
            raise ValueError("Live host identity differs before telemetry firewall update")
        before = host.run(["sudo", "cat", "/etc/klip/firewall.rules"])
        desired = reviewed_rules(config, before)
        before_sha = hashlib.sha256(before.encode()).hexdigest()
        after_sha = hashlib.sha256(desired.encode()).hexdigest()
        path = root / (name + ".rules")
        write_private(path, desired)
        previous = root / (name + ".before.rules")
        if not previous.exists():
            write_private(previous, before)
        inventory = root / (name + ".inventory.yaml")
        write_private(
            inventory,
            yaml.safe_dump(
                {
                    "all": {
                        "hosts": {
                            name: {
                                **fields,
                                "klip_node": config,
                                "klip_firewall_file": str(path),
                                "klip_firewall_before_sha256": before_sha,
                                "klip_firewall_after_sha256": after_sha,
                            }
                        }
                    }
                }
            ),
        )
        record["nodes"][name] = {
            "uid": node["metadata"]["uid"],
            "instance_id": config["instance_id"],
            "before_sha256": before_sha,
            "after_sha256": after_sha,
            "stage": "applying",
        }
        write_private(root / "operation.json", record)
        if before != desired:
            ansible("host-firewall", inventory=inventory, log_path=root / (name + ".ansible.log"))
        after = host.run(["sudo", "cat", "/etc/klip/firewall.rules"])
        actual = json.loads(kubectl("get", "node", name, "-o", "json"))
        if after != desired or actual["metadata"]["uid"] != node["metadata"]["uid"]:
            raise ValueError("Firewall contents or Node identity changed during rollout")
        health = Host(hosts["cp-01"]).agent("etcd")
        if len(health["health"]) != 3 or not all(item["health"] for item in health["health"]):
            raise ValueError("Stop firewall rollout: original etcd quorum is not healthy")
        healthy_targets()
        record["nodes"][name].update(stage="completed", completed_at=utc_now())
        write_private(root / "operation.json", record)
    record["completed_at"] = utc_now()
    write_private(root / "operation.json", record)
    return record


if __name__ == "__main__":
    with (private_dir(state_home() / "cluster") / "operator.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(json.dumps(deploy(), indent=2))
