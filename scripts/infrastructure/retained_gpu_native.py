"""Single-attempt retained-host join, embedded in an operator-owned Kubernetes Job."""

import fcntl
import hashlib
import json
import os
import re
import shlex
import traceback
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from cluster import Host
from cluster_models import PeerRegistry, WorkerRequest
from cluster_workers import store_host
from common import utc_now, write_private
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from platform_management.integrations.job_runner import frozen_file


def require(condition, message):
    if not condition:
        raise ValueError(message)


root = Path("/executor/state")
value = json.loads(os.environ["KLIP_RETAINED_INPUT"])
identity = value["retained"]["instance_id"]
require(identity == "8f2e29b7279b42148ffaaed4a4dcdd8e", "Retained host check failed at stage 18")
require(
    value["retained"]["automatic_termination"] is False, "Retained host check failed at stage 19"
)
require(
    value["retained"]["automatic_restart"] is False and value["retained"]["lease"] is None,
    "Retained host check failed at stage 20",
)
name = value["node"]["name"]
require(name == "writer-a10-retained", "Retained host check failed at stage 22")
directory = root / "retained" / identity
previous = os.environ.get("KLIP_RETAINED_PREVIOUS", "")
if previous:
    prior = directory / "join-result.json"
    require(hashlib.sha256(prior.read_bytes()).hexdigest() == previous, "Prior result differs")
    prior_result = json.loads(prior.read_text())
    require(
        prior_result.get("status") == "failed" and not prior_result.get("node_uid"),
        "Recovery requires failed pre-join execution",
    )
    directory = directory / "recoveries" / os.environ["KLIP_RETAINED_ATTEMPT"]
directory.mkdir(parents=True, exist_ok=True)
result_path = directory / "join-result.json"
with ExitStack() as stack:
    for lock_name in ("dispatch.lock", "cluster/operator.lock"):
        lock = stack.enter_context((root / lock_name).open("a"))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    frozen_file(directory / "input.json", json.dumps(value, sort_keys=True).encode())
    if result_path.exists():
        raise ValueError("Existing retained attempt must be observed, never restarted")
    result = {
        "status": "running",
        "stage": "validating",
        "started_at": utc_now(),
        "instance_id": identity,
        "ownership": "external-user-retained",
        "automatic_termination": False,
        "automatic_restart": False,
        "lease": None,
        "job_uid": os.environ["JOB_UID"],
        "pod_uid": os.environ["POD_UID"],
    }
    write_private(result_path, result)
    token = None
    try:
        for path in (root / "executions").glob("host-*/task.json"):
            task = json.loads(path.read_text())
            if task.get("provider_instance_id") == identity:
                raise ValueError("Retained VM must not belong to a provider-managed task")
        from platform_management.host_executor.worker import scope

        scoped, registry, cp = scope(root)
        expected_names = {"cp-01", "cp-02", "cp-03", "platform-01", "platform-02", "platform-03"}
        if previous:
            expected_names.add(name)
        require(
            set(registry.peers) == expected_names,
            "Retained host check failed at stage 55",
        )
        public = value["public_ip"]
        pins = root / "cluster/known-hosts" / identity
        frozen_file(pins, (public + " " + value["trusted_host_key"] + "\n").encode())
        fields = {
            "ansible_host": public,
            "ansible_user": "ubuntu",
            "ansible_ssh_private_key_file": "/run/secrets/host-executor/lambda-ssh-key",
            "ansible_ssh_common_args": "-o IdentitiesOnly=yes -o StrictHostKeyChecking=yes "
            "-o UserKnownHostsFile=" + shlex.quote(str(pins)),
            "klip_node": value["node"],
        }
        host = Host(fields)
        observed = json.loads(
            host.run(["sudo", "cat", "/etc/klip/retained-owner.json"], secret=True)
        )
        require(observed == value["retained"], "Retained host check failed at stage 79")
        require(
            host.run(["cat", "/proc/sys/kernel/random/boot_id"]).strip() == observed["boot_id"],
            "Retained host check failed at stage 80",
        )
        require(
            host.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,driver_version,memory.total,compute_cap",
                    "--format=csv,noheader",
                ]
            ).strip()
            == observed["gpu"],
            "Retained host check failed at stage 81",
        )
        require(
            host.agent("wg-key")["public_key"] == value["public_wireguard_key"],
            "Retained host check failed at stage 91",
        )
        node = cp.run(
            [
                "sudo",
                "kubectl",
                "--kubeconfig=/etc/kubernetes/admin.conf",
                "get",
                "node",
                name,
                "--ignore-not-found",
                "-o",
                "json",
            ]
        )
        require(not node.strip(), "Retained host check failed at stage 105")
        from cluster_config import firewall

        rules = firewall(value["node"]).replace(
            "-A KLIP-HOST -j DROP",
            "-A KLIP-HOST ! -i wg0 -p tcp -m multiport --dports 22,8888 -j RETURN\n"
            "-A KLIP-HOST -j DROP",
        )
        path = root / "cluster" / ("firewall-" + name + ".rules")
        write_private(path, rules)
        fields["klip_firewall_file"] = str(path)
        store_host(name, fields)
        updated = registry.model_dump(mode="json")
        peer = {
            "instance_id": identity,
            "role": "worker",
            "public_ip": public,
            "private_ip": value["node"]["private_ip"],
            "wireguard_ip": value["node"]["wireguard_ip"],
            "public_key": value["public_wireguard_key"],
        }
        if previous:
            require(updated["peers"][name] == peer, "Prior peer differs")
        else:
            updated["peers"][name] = peer
            updated["revision"] += 1
        updated = PeerRegistry.model_validate(updated)
        write_private(root / "cluster/peers.json", updated.model_dump(mode="json"))
        token = cp.run(
            [
                "sudo",
                "kubeadm",
                "token",
                "create",
                "--ttl",
                "10m",
                "--description",
                "klip-retained-a10",
            ],
            secret=True,
        ).strip()
        require(
            re.fullmatch("[a-z0-9]{6}\\.[a-z0-9]{16}", token),
            "Retained host check failed at stage 146",
        )
        cert = x509.load_pem_x509_certificate((root / "cluster/ca.crt").read_bytes())
        pub = cert.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        bundle = directory / "join-credential.json"
        write_private(
            bundle,
            {
                "expires_at": (datetime.now(UTC) + timedelta(minutes=9)).isoformat(),
                "discovery": {
                    "apiServerEndpoint": value["node"]["api_endpoint"].removeprefix("https://"),
                    "token": token,
                    "caCertHashes": ["sha256:" + hashlib.sha256(pub).hexdigest()],
                },
            },
        )
        ref = "credential://klip/retained-a10-join"
        aliases = root / "cluster/join-references.json"
        previous = json.loads(aliases.read_text()) if aliases.exists() else {}
        write_private(aliases, {**previous, ref: str(bundle)})
        request = WorkerRequest(
            operation_id="retained-a10-join",
            deadline=datetime.now(UTC) + timedelta(minutes=15),
            provider="lambda",
            provider_instance_id=identity,
            node_name=name,
            quarantine=True,
            public_ip=public,
            private_ip=value["node"]["private_ip"],
            wireguard_ip=value["node"]["wireguard_ip"],
            access_ref="credential://klip/retained-a10",
            join_ref=ref,
            registry_revision=updated.revision,
        )
        request_path = directory / "join-request.json"
        frozen_file(request_path, request.model_dump_json().encode())
        result.update(stage="joining", registry_revision=updated.revision)
        write_private(result_path, result)
        from cluster import ansible, approve_serving_csrs, kubectl

        ansible(
            "mesh",
            extra={"klip_ca_bundle": str(root / "cluster/ca.crt")},
            log_path=directory / "mesh.log",
        )
        result.update(stage="mesh-reconciled")
        write_private(result_path, result)
        host.agent("route")
        document = {
            "apiVersion": "kubeadm.k8s.io/v1beta4",
            "kind": "JoinConfiguration",
            "nodeRegistration": {
                "name": name,
                "criSocket": value["node"]["cri_socket"],
                "taints": [{"key": "klip.dev/bootstrap", "value": "true", "effect": "NoSchedule"}],
                "kubeletExtraArgs": [
                    {"name": "node-ip", "value": value["node"]["wireguard_ip"]},
                    {"name": "provider-id", "value": "lambda:///" + identity},
                ],
            },
            "discovery": {"bootstrapToken": json.loads(bundle.read_text())["discovery"]},
        }
        host.put("/etc/klip/retained-join.yaml", yaml.safe_dump(document))
        try:
            host.run(
                ["sudo", "kubeadm", "join", "--config", "/etc/klip/retained-join.yaml"],
                timeout=600,
                secret=True,
            )
        finally:
            host.run(["sudo", "rm", "-f", "/etc/klip/retained-join.yaml"])
        kubectl("wait", "--for=condition=Ready", "node/" + name, "--timeout=300s")
        node = json.loads(kubectl("get", "node", name, "-o", "json"))
        require(
            node["spec"]["providerID"] == "lambda:///" + identity,
            "Retained host check failed at stage 220",
        )
        from cluster_models import assert_node_identity

        assert_node_identity(request, node)
        approve_serving_csrs()
        pods = json.loads(
            kubectl(
                "-n",
                "kube-system",
                "get",
                "pods",
                "-l",
                "k8s-app=cilium",
                "--field-selector",
                "spec.nodeName=" + name,
                "-o",
                "json",
            )
        )["items"]
        require(len(pods) == 1, "Retained host check failed at stage 239")
        kubectl(
            "-n",
            "kube-system",
            "wait",
            "--for=condition=Ready",
            "pod/" + pods[0]["metadata"]["name"],
            "--timeout=180s",
        )
        joined = {
            "node_uid": node["metadata"]["uid"],
            "registry_revision": updated.revision,
            "readiness": {
                "host": True,
                "wireguard": True,
                "kubernetes": True,
                "cilium": True,
                "gpu_runtime": False,
            },
        }
        require(
            host.run(["cat", "/proc/sys/kernel/random/boot_id"]).strip() == observed["boot_id"],
            "Retained host check failed at stage 259",
        )
        result.update(
            status="completed",
            stage="joined-quarantined",
            completed_at=utc_now(),
            node_uid=joined["node_uid"],
            registry_revision=joined["registry_revision"],
            readiness=joined["readiness"],
        )
    except Exception as error:
        result.update(
            status="failed",
            stage="requires-observation",
            error_type=type(error).__name__,
            frames=[
                {"file": x.filename.rsplit("/", 1)[-1], "line": x.lineno}
                for x in traceback.extract_tb(error.__traceback__)
            ],
            completed_at=utc_now(),
        )
        raise
    finally:
        if token and re.fullmatch("[a-z0-9]{6}\\.[a-z0-9]{16}", token):
            cp.run(["sudo", "kubeadm", "token", "delete", token.split(".")[0]], secret=True)
        (directory / "join-credential.json").unlink(missing_ok=True)
        write_private(result_path, result)
