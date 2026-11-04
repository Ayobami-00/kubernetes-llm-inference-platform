"""Revoke a quarantined bootstrap only after management confirms its VM is fenced.

These operations use the existing serialized host-task owner. They never contact
the terminated GPU host and never provide an alternative to draining a ready node.
"""

import hashlib
import json
from datetime import UTC, datetime

from cluster_models import PeerRegistry, assert_node_identity
from common import utc_now, write_private


def checked_peer(registry, task):
    expected = {
        "instance_id": task.provider_instance_id,
        "role": "worker",
        "public_ip": str(task.public_ip),
        "private_ip": str(task.private_ip),
        "wireguard_ip": str(task.wireguard_ip),
    }
    for name, peer in registry.model_dump(mode="json")["peers"].items():
        if name == task.node_name:
            if any(peer.get(k) != value for k, value in expected.items()):
                raise ValueError("Bootstrap peer differs from the trusted allocation")
        elif any(peer[k] == expected[k] for k in ("instance_id", "wireguard_ip", "public_ip")):
            raise ValueError("Bootstrap allocation collides with another registered peer")


def kubectl(cp, *arguments):
    return cp.run(["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", *arguments])


def bootstrap_evidence(cp, task, *, cordon=False):
    raw = kubectl(cp, "get", "node", task.node_name, "--ignore-not-found", "-o", "json")
    node = json.loads(raw) if raw.strip() else None
    evidence = {"node": None, "pods": [], "observed_at": utc_now()}
    pods = json.loads(
        kubectl(
            cp,
            "get",
            "pods",
            "-A",
            "--field-selector",
            "spec.nodeName=" + task.node_name,
            "-o",
            "json",
        )
    )["items"]
    for pod in pods:
        # Static/mirror Pods and ordinary Jobs are not daemon workloads.
        if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"} and not any(
            o.get("kind") == "DaemonSet" and o.get("controller") is True
            for o in pod["metadata"].get("ownerReferences", [])
        ):
            raise ValueError("Bootstrap worker has active workloads; drain is required")
    evidence["pods"] = [{k: p["metadata"][k] for k in ("namespace", "name", "uid")} for p in pods]
    if node is not None:
        assert_node_identity(task, node)
        if node["spec"].get("providerID") != "lambda:///" + task.provider_instance_id:
            raise ValueError("Bootstrap cleanup requires the exact Lambda provider identity")
        if not any(
            t.get("key") == "klip.dev/bootstrap"
            and t.get("value") == "true"
            and t.get("effect") == "NoSchedule"
            for t in node["spec"].get("taints", [])
        ):
            raise ValueError("Ready or unquarantined workers require normal drain and removal")
        if cordon:
            patch = [
                {"op": "test", "path": "/metadata/uid", "value": node["metadata"]["uid"]},
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": node["metadata"]["resourceVersion"],
                },
                {"op": "add", "path": "/spec/unschedulable", "value": True},
            ]
            kubectl(cp, "patch", "node", task.node_name, "--type=json", "-p", json.dumps(patch))
        elif not node["spec"].get("unschedulable"):
            raise ValueError("Bootstrap safety cordon changed before fenced revocation")
        evidence["node"] = {k: node["metadata"][k] for k in ("name", "uid", "resourceVersion")}
    return evidence


def perform_cleanup(root, task, registry, cp):
    from cluster import ansible, assert_peer_writer, load_hosts
    from cluster_workers import delete_node, remove_host

    assert_peer_writer()
    checked_peer(registry, task)
    host = load_hosts().get(task.node_name)
    if host is not None and (
        host.get("ansible_host") != str(task.public_ip)
        or any(
            host.get("klip_node", {}).get(field) != expected
            for field, expected in {
                "instance_id": task.provider_instance_id,
                "wireguard_ip": str(task.wireguard_ip),
            }.items()
        )
    ):
        raise ValueError("Bootstrap inventory differs from the trusted allocation")
    evidence = bootstrap_evidence(cp, task, cordon=task.action == "cleanup-check")
    if task.action == "revoke-bootstrap":
        if task.provider_fence is None or task.provider_fence.confirmed_at > datetime.now(UTC):
            raise ValueError("Bootstrap revocation requires a confirmed provider fence")
        document = registry.model_dump(mode="json")
        if document["peers"].pop(task.node_name, None) is not None:
            document["revision"] += 1
        registry = PeerRegistry.model_validate(document)
        # Persist revocation before reconciliation; failure leaves the address reserved.
        write_private(root / "cluster/peers.json", registry.model_dump(mode="json"))
        remove_host(task.node_name)
        ansible(
            "mesh",
            extra={"klip_ca_bundle": str(root / "cluster/ca.crt")},
            log_path=root / "executions" / task.id / "mesh.log",
        )
        if evidence["node"] is not None:
            # Always use the observed UID, including failed joins without an earlier UID.
            delete_node(task.model_copy(update={"node_uid": evidence["node"]["uid"]}))
        evidence["provider_fence"] = task.provider_fence.model_dump(mode="json")
    evidence["registry_revision"] = registry.revision
    raw = json.dumps(evidence, sort_keys=True).encode()
    write_private(root / "executions" / task.id / "cleanup-evidence.json", evidence)
    node = evidence["node"]
    return {
        "status": "completed",
        "provider_instance_id": task.provider_instance_id,
        "registry_revision": registry.revision,
        "node_name": node["name"] if node else None,
        "node_uid": node["uid"] if node else None,
        "readiness": dict.fromkeys(("host", "wireguard", "kubernetes", "cilium"), False),
        "evidence_sha256": hashlib.sha256(raw).hexdigest(),
        "bootstrap_fence_permitted": task.action == "cleanup-check",
        "bootstrap_cleanup_complete": task.action == "revoke-bootstrap",
    }
