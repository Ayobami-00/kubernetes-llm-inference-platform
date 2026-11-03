"""Provider-neutral host operations. Allocation, termination and durable jobs stay with C."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml
from cluster_config import firewall, kubeadm_documents, node_configuration
from cluster_models import PeerRegistry, WorkerRequest, assert_node_identity
from common import CONFIG, PROFILES, ROOT, Aws, state_home, tf, utc_now, write_private


class WorkerOperationFailed(RuntimeError):
    def __init__(self, result: dict):
        self.operation_result = result
        super().__init__("Worker operation failed; structured result retained")


def resolve_access(reference: str) -> dict:
    aliases = json.loads((state_home() / "cluster/credential-references.json").read_text())
    value = aliases.get(reference)
    required = {
        "ansible_host",
        "ansible_user",
        "ansible_ssh_private_key_file",
        "ansible_ssh_common_args",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("Host access reference must resolve to a strictly pinned SSH transport")
    if (
        "StrictHostKeyChecking=yes" not in value["ansible_ssh_common_args"]
        or "UserKnownHostsFile=" not in value["ansible_ssh_common_args"]
    ):
        raise ValueError("Strict host-key pinning is required")
    return value


def store_host(name: str, fields: dict) -> None:
    from cluster import FOLDER

    path = FOLDER / "inventory.yaml"
    inventory = yaml.safe_load(path.read_text())
    inventory["all"]["children"].setdefault("external", {"hosts": {}})["hosts"][name] = fields
    write_private(path, yaml.safe_dump(inventory, sort_keys=False))
    write_private(FOLDER / f"prepared/{name}.json", fields)


def remove_host(name: str) -> None:
    from cluster import FOLDER

    path = FOLDER / "inventory.yaml"
    inventory = yaml.safe_load(path.read_text())
    inventory["all"]["children"].get("external", {}).get("hosts", {}).pop(name, None)
    write_private(path, yaml.safe_dump(inventory, sort_keys=False))


def register_probe() -> dict:
    from cluster import FOLDER, Host, ansible
    from common import ensure_ssh_key
    from smoke import ssm_command, wait_until

    aws = Aws(PROFILES["foundation"])
    aws.preflight("foundation")
    probe = json.loads(tf("probe", "output", "-json", "probe"))
    if probe["cidr"] != "10.41.0.0/24":
        raise ValueError("Unexpected probe network")
    wait_until(
        lambda: any(
            item.get("PingStatus") == "Online"
            for item in aws.call(
                "ssm",
                "describe-instance-information",
                "--filters",
                json.dumps([{"Key": "InstanceIds", "Values": [probe["instance_id"]]}]),
            )["InstanceInformationList"]
        ),
        "probe SSM registration",
        timeout=180,
    )
    keys = ssm_command(aws, probe["instance_id"], "cat /etc/ssh/ssh_host_ed25519_key.pub")[
        "stdout"
    ].strip()
    known = FOLDER / "probe-known-hosts"
    write_private(known, f"{probe['instance_id']} {keys}\n")
    _, key = ensure_ssh_key()
    import shlex

    fields = {
        "ansible_host": probe["instance_id"],
        "ansible_user": "ubuntu",
        "ansible_ssh_private_key_file": str(key),
        "ansible_ssh_common_args": "-o StrictHostKeyChecking=yes "
        f"-o UserKnownHostsFile={shlex.quote(str(known))} "
        "-o ProxyCommand="
        + shlex.quote(
            shlex.join(
                [
                    "aws",
                    "ssm",
                    "start-session",
                    "--target",
                    "%h",
                    "--document-name",
                    "AWS-StartSSHSession",
                    "--parameters",
                    "portNumber=%p",
                    "--profile",
                    PROFILES["foundation"],
                    "--region",
                    CONFIG["region"],
                ]
            )
        ),
    }
    allocation = json.loads((state_home() / "reports/foundation.json").read_text())["allocation"]
    fields["klip_node"] = node_configuration("network-probe", allocation, probe, role="probe")
    rules = FOLDER / "firewall-network-probe.rules"
    write_private(rules, firewall(fields["klip_node"]))
    fields["klip_firewall_file"] = str(rules)
    store_host("network-probe", fields)
    # Prepare just this host, never reconfigure healthy control-plane runtimes for a probe.
    single = {
        "all": {
            "vars": {
                "klip_repository": str(ROOT),
                "klip_peer_registry": str(FOLDER / "peers.json"),
                "klip_ca_bundle": str(FOLDER / "ca.crt"),
            },
            "children": {"external": {"hosts": {"network-probe": fields}}},
        }
    }
    path = FOLDER / "probe-inventory.yaml"
    write_private(path, yaml.safe_dump(single))
    ansible("prepare", inventory=path)
    public = Host(fields).agent("wg-key")["public_key"]
    registry = PeerRegistry.model_validate_json((FOLDER / "peers.json").read_text()).model_dump(
        mode="json"
    )
    registry["revision"] += 1
    registry["peers"]["network-probe"] = {
        key: probe[key] for key in ("instance_id", "public_ip", "private_ip", "wireguard_ip")
    }
    registry["peers"]["network-probe"].update(role="probe", public_key=public)
    write_private(
        FOLDER / "peers.json", PeerRegistry.model_validate(registry).model_dump(mode="json")
    )
    ansible("mesh", extra={"klip_ca_bundle": str(FOLDER / "ca.crt")})
    return {"registered": "network-probe", "instance_id": probe["instance_id"]}


def delete_node(request: WorkerRequest) -> None:
    from cluster import Host, load_hosts

    # DeleteOptions UID precondition closes the get/delete race for reused names.
    program = r"""
import base64, http.client, json, ssl, subprocess, sys, tempfile
from pathlib import Path
request = json.loads(sys.stdin.read())
config = json.loads(subprocess.check_output([
    'kubectl', '--kubeconfig=/etc/kubernetes/admin.conf',
    'config', 'view', '--raw', '-o', 'json'
]))
with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    cluster = config['clusters'][0]['cluster']
    user = config['users'][0]['user']
    for key, field in [('ca', 'certificate-authority-data'),
                       ('cert', 'client-certificate-data'), ('key', 'client-key-data')]:
        (root/key).write_bytes(base64.b64decode((cluster if key == 'ca' else user)[field]))
    context = ssl.create_default_context(cafile=str(root/'ca'))
    context.load_cert_chain(str(root/'cert'), str(root/'key'))
    connection = http.client.HTTPSConnection('10.200.0.11', 6443, context=context, timeout=10)
    body = {'apiVersion': 'v1', 'kind': 'DeleteOptions',
            'preconditions': {'uid': request['node_uid']}}
    connection.request('DELETE', '/api/v1/nodes/' + request['node_name'],
                       json.dumps(body), {'Content-Type': 'application/json'})
    response = connection.getresponse()
    response.read()
    if response.status not in (200, 202, 404):
        raise RuntimeError('UID-guarded deletion failed: ' + str(response.status))
"""
    Host(load_hosts()["cp-01"]).run(
        ["sudo", "python3", "-c", program], data=request.model_dump_json(), secret=True
    )


def operate_worker(action: str, request_path: Path | None) -> dict:
    from cluster import FOLDER, Host, ansible, kubectl, load_hosts

    if action == "probe-register":
        return register_probe()
    if action == "probe-cleanup":
        registry = PeerRegistry.model_validate_json((FOLDER / "peers.json").read_text()).model_dump(
            mode="json"
        )
        registry["peers"].pop("network-probe", None)
        registry["revision"] += 1
        write_private(FOLDER / "peers.json", registry)
        remove_host("network-probe")
        ansible("mesh", extra={"klip_ca_bundle": str(FOLDER / "ca.crt")})
        return {"peers_revoked": True, "terraform_probe_destroy_required": True}
    if not request_path:
        raise ValueError("A versioned --request is required")
    request = WorkerRequest.model_validate_json(request_path.read_text())
    operation = FOLDER / "operations" / f"{request.operation_id}.json"
    digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    if operation.exists():
        previous = json.loads(operation.read_text())
        if previous["request_sha256"] != digest or previous["action"] != action:
            raise ValueError("Idempotency key reused with different inputs/action")
        if previous["status"] == "completed":
            return previous
    registry = PeerRegistry.model_validate_json((FOLDER / "peers.json").read_text())
    expected_revision = (
        previous.get("registry_revision", request.registry_revision)
        if operation.exists()
        else request.registry_revision
    )
    if registry.revision != expected_revision:
        raise ValueError("Stale peer registry revision")
    request.remaining()
    result = {
        "schema_version": "klip-worker-result.v1",
        "operation_id": request.operation_id,
        "action": action,
        "request_sha256": digest,
        "provider_instance_id": request.provider_instance_id,
        "status": "running",
        "stages": [],
        "readiness": {
            "host": False,
            "wireguard": False,
            "kubernetes": False,
            "cilium": False,
            "gpu_runtime": False,
        },
        "registry_revision": registry.revision,
        "cleanup_pending": False,
        "address_reusable": False,
    }
    write_private(operation, result)
    import cluster

    cluster.OPERATION_DEADLINE = request.deadline.timestamp()

    def stage(name: str) -> None:
        result["stages"].append(name)
        write_private(operation, result)

    try:
        if action == "worker-prepare":
            if request.provider == "lambda" and not request.ssh_source_cidrs:
                raise ValueError(
                    "Declare the automation executor's public /32 SSH sources for Lambda"
                )
            fields = resolve_access(request.access_ref)
            allocation = json.loads((state_home() / "reports/foundation.json").read_text())[
                "allocation"
            ]
            node = {
                "instance_id": request.provider_instance_id,
                "public_ip": str(request.public_ip),
                "private_ip": str(request.private_ip),
                "wireguard_ip": str(request.wireguard_ip),
            }
            if request.availability_zone is not None:
                node["availability_zone"] = request.availability_zone
            fields["klip_node"] = node_configuration(
                request.node_name, allocation, node, role="worker", provider=request.provider
            )
            fields["klip_node"]["ssh_source_cidrs"] = [
                str(network) for network in request.ssh_source_cidrs
            ]
            fields["klip_node"]["quarantine"] = request.quarantine
            rules = FOLDER / f"firewall-{request.node_name}.rules"
            write_private(rules, firewall(fields["klip_node"]))
            fields["klip_firewall_file"] = str(rules)
            store_host(request.node_name, fields)
            single = {
                "all": {
                    "vars": {"klip_repository": str(ROOT)},
                    "children": {"external": {"hosts": {request.node_name: fields}}},
                }
            }
            path = FOLDER / f"prepare-{request.node_name}.yaml"
            write_private(path, yaml.safe_dump(single))
            ansible(
                "prepare",
                inventory=path,
                log_path=FOLDER / "operations" / f"{request.operation_id}-prepare.log",
            )
            stage("host_prepared")
            result["public_key"] = Host(fields).agent("wg-key")["public_key"]
            result["readiness"]["host"] = True
        else:
            peer = registry.peers.get(request.node_name)
            if peer and (
                peer.instance_id != request.provider_instance_id
                or str(peer.wireguard_ip) != str(request.wireguard_ip)
            ):
                raise ValueError("Peer identity changed; operation cannot revoke another worker")
            fields = load_hosts().get(request.node_name) or json.loads(
                (FOLDER / f"prepared/{request.node_name}.json").read_text()
            )
            if fields["klip_node"]["instance_id"] != request.provider_instance_id:
                raise ValueError("Prepared host belongs to another provider instance")
            host = Host(fields)
            if action == "worker-join":
                peer = registry.peers.get(request.node_name)
                if (
                    not peer
                    or peer.instance_id != request.provider_instance_id
                    or str(peer.wireguard_ip) != str(request.wireguard_ip)
                ):
                    raise ValueError("Reconcile this worker's direct peers before join")
                ansible(
                    "mesh",
                    extra={"klip_ca_bundle": str(FOLDER / "ca.crt")},
                    log_path=FOLDER / "operations" / f"{request.operation_id}-mesh.log",
                )
                stage("mesh_reconciled")
                host.agent("route")
                current = kubectl(
                    "get", "node", request.node_name, "--ignore-not-found", "-o", "json"
                )
                if not current.strip():
                    if not request.join_ref:
                        raise ValueError("A short-lived join credential reference is required")
                    aliases = json.loads((FOLDER / "join-references.json").read_text())
                    bundle = json.loads(Path(aliases[request.join_ref]).read_text())
                    if datetime.fromisoformat(bundle["expires_at"]) <= datetime.now(UTC):
                        raise ValueError("Join credential expired")
                    if bundle["discovery"]["apiServerEndpoint"] != fields["klip_node"][
                        "api_endpoint"
                    ].removeprefix("https://"):
                        raise ValueError("Join bundle targets another cluster endpoint")
                    path = "/etc/klip/worker-join.yaml"
                    host.put(
                        path,
                        yaml.safe_dump_all(
                            kubeadm_documents(
                                fields["klip_node"], join=bundle["discovery"], control_plane=False
                            )
                        ),
                    )
                    try:
                        host.run(
                            ["sudo", "kubeadm", "join", "--config", path],
                            timeout=min(request.remaining(), 600),
                            secret=True,
                        )
                    finally:
                        host.run(["sudo", "rm", "-f", path])
                node = json.loads(kubectl("get", "node", request.node_name, "-o", "json"))
                assert_node_identity(request, node)
                kubectl(
                    "wait",
                    "--for=condition=Ready",
                    "node/" + request.node_name,
                    "--timeout=" + str(min(request.remaining(), 300)) + "s",
                )
                after = json.loads(kubectl("get", "node", request.node_name, "-o", "json"))
                assert_node_identity(request, after)
                if after["metadata"]["uid"] != node["metadata"]["uid"]:
                    raise ValueError("Worker UID changed while waiting for readiness")
                pods = json.loads(
                    kubectl(
                        "-n",
                        "kube-system",
                        "get",
                        "pods",
                        "-l",
                        "k8s-app=cilium",
                        "--field-selector",
                        "spec.nodeName=" + request.node_name,
                        "-o",
                        "json",
                    )
                )["items"]
                if len(pods) != 1:
                    raise ValueError("Expected exactly one worker Cilium agent")
                kubectl(
                    "-n",
                    "kube-system",
                    "wait",
                    "--for=condition=Ready",
                    "pod/" + pods[0]["metadata"]["name"],
                    "--timeout=" + str(min(request.remaining(), 300)) + "s",
                )
                from cluster import approve_serving_csrs

                for attempt in range(30):
                    approve_serving_csrs()
                    try:
                        host.run(
                            [
                                "sudo",
                                "openssl",
                                "verify",
                                "-CAfile",
                                "/etc/kubernetes/pki/ca.crt",
                                "/var/lib/kubelet/pki/kubelet-server-current.pem",
                            ]
                        )
                        break
                    except RuntimeError:
                        if attempt == 29:
                            raise
                        time.sleep(1)
                stage("serving_certificate_verified")
                kubectl(
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
                result.update(node_name=request.node_name, node_uid=node["metadata"]["uid"])
                result["stages"].extend(["kubernetes_joined", "node_identity_verified"])
                result["readiness"].update(host=True, wireguard=True, kubernetes=True, cilium=True)
            elif action == "worker-remove":
                if not request.node_uid:
                    raise ValueError("Removal requires the observed Kubernetes UID")
                current = kubectl(
                    "get", "node", request.node_name, "--ignore-not-found", "-o", "json"
                )
                if current.strip():
                    assert_node_identity(request, json.loads(current))
                    kubectl(
                        "drain",
                        request.node_name,
                        "--ignore-daemonsets",
                        "--delete-emptydir-data",
                        "--timeout=" + str(min(request.remaining(), request.drain_timeout_s)) + "s",
                    )
                host.run(["sudo", "systemctl", "disable", "--now", "kubelet"])
                result["stages"].extend(["drained", "kubelet_stopped"])
                host.run(["sudo", "systemctl", "disable", "--now", "klip-api-route.timer"])
                host.run(["sudo", "systemctl", "disable", "wg-quick@wg0"])
                host.run(
                    [
                        "sudo",
                        "python3",
                        "-c",
                        "from pathlib import Path; import subprocess; "
                        "subprocess.run(['wg-quick','down','wg0'],check=True) "
                        "if Path('/sys/class/net/wg0').exists() else None",
                    ]
                )
                updated = registry.model_dump(mode="json")
                if updated["peers"].pop(request.node_name, None):
                    updated["revision"] += 1
                write_private(FOLDER / "peers.json", updated)
                result["registry_revision"] = updated["revision"]
                write_private(operation, result)
                remove_host(request.node_name)
                ansible(
                    "mesh",
                    extra={"klip_ca_bundle": str(FOLDER / "ca.crt")},
                    log_path=FOLDER / "operations" / f"{request.operation_id}-mesh.log",
                )
                stage("peers_revoked")
                if current.strip():
                    delete_node(request)
                stage("node_removed")
                result["provider_termination_required"] = True
        result.update(
            status="completed",
            completed_at=utc_now(),
            registry_revision=json.loads((FOLDER / "peers.json").read_text())["revision"],
        )
    except Exception as error:
        result.update(status="failed", cleanup_pending=True, failed_at=utc_now())
        result["error"] = {
            "code": type(error).__name__,
            "message": "Host operation failed; inspect the executor's private logs",
        }
        write_private(operation, result)
        raise WorkerOperationFailed(result) from error
    finally:
        cluster.OPERATION_DEADLINE = None
    write_private(operation, result)
    return result
