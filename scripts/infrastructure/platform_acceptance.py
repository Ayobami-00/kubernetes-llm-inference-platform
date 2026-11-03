"""Live CPU-worker Pod/DNS/MTU and metadata-isolation checks; no GPU or serving claims."""

from __future__ import annotations

import json
import uuid

import yaml
from cluster_verification import ready_node
from common import ROOT, utc_now, write_private


def metadata_connection_blocked(message: str) -> bool:
    # agnhost's connect command emits the literal TIMEOUT sentinel for a dropped
    # TCP connection. Kubernetes transport/authentication errors must not pass.
    if message.strip().splitlines() == ["TIMEOUT", "command terminated with exit code 1"]:
        return True
    message = message.lower()
    return "169.254.169.254" in message and any(
        part in message
        for part in (
            "connection refused",
            "network is unreachable",
            "operation not permitted",
        )
    )


def verify_platform() -> dict:
    from cluster import FOLDER, Host, kubectl, load_hosts

    verified = json.loads((FOLDER / "platform-workers.json").read_text())["nodes"]
    slots = json.loads((ROOT / "infra/terraform/platform.json").read_text())["nodes"]
    if set(verified) != set(slots):
        raise ValueError("All three platform workers need completed join observations")
    hosts = load_hosts()
    observed = {
        node["metadata"]["name"]: node
        for node in json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    }
    for name, slot in slots.items():
        node = observed[name]
        ready_node(node, verified[name]["instance_id"], slot["wireguard_ip"])
        if node["metadata"]["uid"] != verified[name]["node_uid"]:
            raise ValueError("Platform node UID changed since join")
        if node["metadata"].get("labels", {}).get("klip.dev/node-role") != "platform":
            raise ValueError("Platform placement label missing")
        if any(t["key"] == "klip.dev/bootstrap" for t in node["spec"].get("taints", [])):
            raise ValueError("Platform worker remains quarantined")
    identity = uuid.uuid4().hex[:12]
    namespace = "klip-platform-check-" + identity
    fixture = list(yaml.safe_load_all((ROOT / "deploy/releases/smoke.yaml").read_text()))
    fixture[0]["metadata"]["name"] = namespace
    fixture[0]["metadata"]["annotations"] = {"klip.dev/verification-id": identity}
    for document in fixture[1:]:
        document["metadata"]["namespace"] = namespace
        if document["kind"] == "Deployment":
            spec = document["spec"]["template"]["spec"]
            spec["nodeSelector"] = {"klip.dev/node-role": "platform"}
            spec["tolerations"] = []
    cp = Host(hosts["cp-01"])
    command = ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf"]
    created = json.loads(
        cp.run([*command, "create", "-f", "-", "-o", "json"], data=yaml.safe_dump(fixture[0]))
    )
    uid = created["metadata"]["uid"]
    report = {
        "schema_version": "klip-platform-network-verification.v1",
        "namespace": namespace,
        "namespace_uid": uid,
        "status": "running",
        "nodes": {},
    }
    path = FOLDER / "platform-network-verification.json"

    def save_report() -> None:
        write_private(FOLDER / "operations" / f"platform-network-{identity}.json", report)
        write_private(path, report)

    save_report()
    try:
        cp.run([*command, "apply", "-f", "-"], data=yaml.safe_dump_all(fixture[1:]))
        kubectl("-n", namespace, "rollout", "status", "deployment/smoke", "--timeout=180s")
        pods = json.loads(kubectl("-n", namespace, "get", "pods", "-l", "app=smoke", "-o", "json"))[
            "items"
        ]
        if len(pods) != 3 or {p["spec"]["nodeName"] for p in pods} != set(slots):
            raise ValueError("Expected one ready test Pod on each reviewed platform worker")
        for pod in pods:
            name, pod_name = pod["spec"]["nodeName"], pod["metadata"]["name"]
            probe = (
                "-n",
                namespace,
                "exec",
                pod_name,
                "--",
                "/agnhost",
                "connect",
                "--protocol=tcp",
                "--timeout=5s",
            )
            report["current_probe"] = {"node": name, "kind": "dns_service"}
            save_report()
            kubectl(*probe, f"smoke.{namespace}.svc.cluster.local:8080")
            for target in pods:
                report["current_probe"] = {
                    "node": name,
                    "kind": "pod_connection",
                    "target_node": target["spec"]["nodeName"],
                }
                save_report()
                kubectl(*probe, target["status"]["podIP"] + ":8080")
            report["current_probe"] = {"node": name, "kind": "metadata_isolation"}
            save_report()
            try:
                kubectl(
                    "-n",
                    namespace,
                    "exec",
                    pod_name,
                    "--",
                    "/agnhost",
                    "connect",
                    "--protocol=tcp",
                    "--timeout=2s",
                    "169.254.169.254:80",
                )
            except RuntimeError as error:
                if not metadata_connection_blocked(str(error)):
                    raise
            else:
                raise ValueError("A platform workload Pod can reach instance metadata")
            report["nodes"][name] = {
                "node_uid": verified[name]["node_uid"],
                "pod_uid": pod["metadata"]["uid"],
                "dns_service": True,
                "pod_targets": len(pods),
                "metadata_denied": True,
                **Host(hosts[name]).agent("pod-network"),
            }
            save_report()
        report.update(status="completed", captured_at=utc_now())
        return report
    except Exception:
        report.update(status="failed", captured_at=utc_now())
        raise
    finally:
        report["cleanup_pending"] = True
        try:
            current = kubectl("get", "namespace", namespace, "--ignore-not-found", "-o", "json")
            if current.strip():
                metadata = json.loads(current)["metadata"]
                if (
                    metadata["uid"] != uid
                    or metadata.get("annotations", {}).get("klip.dev/verification-id") != identity
                ):
                    raise ValueError("Test namespace ownership changed; cleanup refused")
                kubectl("delete", "namespace", namespace, "--wait=true", "--timeout=60s")
            report["cleanup_pending"] = False
        except Exception:
            report["status"] = "failed"
            raise
        finally:
            save_report()
