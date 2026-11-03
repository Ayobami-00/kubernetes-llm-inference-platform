"""Versioned operator workflows for Ansible, kubeadm, peer reconciliation and observations."""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import yaml
from cluster_config import kubeadm_documents, write_ansible_inputs
from cluster_models import PeerRegistry
from common import (
    PROFILES,
    ROOT,
    Aws,
    environment,
    private_dir,
    source_digest,
    state_home,
    tool,
    utc_now,
    write_private,
)
from inventory import FoundationObservation, shared_inventory, verify_ca

FOLDER = state_home() / "cluster"
OPERATION_DEADLINE = None


def operation_timeout(default: int) -> int:
    if OPERATION_DEADLINE is None:
        return default
    remaining = int(OPERATION_DEADLINE - time.time())
    if remaining <= 0:
        raise ValueError("Operation deadline has expired")
    return min(default, remaining)


def checked(
    arguments: list[str], *, data: str | None = None, timeout: int = 600, secret: bool = False
) -> str:
    result = subprocess.run(
        arguments,
        input=data,
        text=True,
        capture_output=True,
        timeout=operation_timeout(timeout),
        env=environment(PROFILES["foundation"]),
        check=False,
    )
    if result.returncode:
        if secret:
            raise RuntimeError(f"{Path(arguments[0]).name} failed; secret-bearing output withheld")
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout


class Host:
    def __init__(self, fields: dict):
        self.fields = fields

    def ssh_arguments(self, *, reuse: bool = True) -> list[str]:
        fields = self.fields
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", fields["ansible_host"]) or not re.fullmatch(
            r"[a-z_][a-z0-9_-]*", fields["ansible_user"]
        ):
            raise ValueError("Invalid SSH host/user")
        # Reuse the authenticated SSM tunnel across short reconciliation commands.
        # Include pins/key/proxy settings in the socket identity, so changed access
        # configuration cannot silently reuse a session authenticated differently.
        multiplex = ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
        if reuse:
            identity = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:16]
            control_path = private_dir(state_home() / "ssh") / identity
            # OpenSSH appends a temporary suffix when creating the master socket.
            if len(str(control_path).encode()) > 85:
                raise ValueError("Operator state path is too long for an SSH control socket")
            multiplex = [
                "-o",
                "ControlMaster=auto",
                "-o",
                "ControlPersist=60",
                "-o",
                f"ControlPath={control_path}",
            ]
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=45",
            *multiplex,
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            "StrictHostKeyChecking=yes",
            "-i",
            fields["ansible_ssh_private_key_file"],
            *shlex.split(fields.get("ansible_ssh_common_args", "")),
            fields["ansible_user"] + "@" + fields["ansible_host"],
        ]

    def run(
        self,
        arguments: list[str],
        *,
        data: str | None = None,
        timeout: int = 600,
        secret: bool = False,
    ) -> str:
        return checked(
            [*self.ssh_arguments(), shlex.join(arguments)],
            data=data,
            timeout=timeout,
            secret=secret,
        )

    def put(self, path: str, data: str) -> None:
        # Input is streamed, never embedded in a command line or shell expansion.
        program = """
from pathlib import Path
import os, sys, tempfile
p = Path(sys.argv[1])
p.parent.mkdir(parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix='.' + p.name + '.', dir=p.parent)
try:
    with os.fdopen(fd, 'w') as stream:
        stream.write(sys.stdin.read())
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, p)
    directory = os.open(p.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    Path(temporary).unlink(missing_ok=True)
"""
        self.run(["sudo", "python3", "-c", program, path], data=data, secret=True)

    def agent(self, action: str) -> dict:
        return json.loads(self.run(["sudo", "python3", "/usr/local/sbin/klip-node", action]))


def load_hosts() -> dict[str, dict]:
    inventory = yaml.safe_load((FOLDER / "inventory.yaml").read_text())
    result = {}
    for group in inventory["all"]["children"].values():
        result.update(group.get("hosts", {}))
    return result


def ansible(
    playbook: str,
    *,
    inventory: Path | None = None,
    extra: dict | None = None,
    limit: str | None = None,
    log_path: Path | None = None,
) -> None:
    arguments = [
        str(ROOT / ".venv/bin/ansible-playbook"),
        "-i",
        str(inventory or FOLDER / "inventory.yaml"),
        str(ROOT / f"infra/ansible/playbooks/{playbook}.yaml"),
    ]
    if limit:
        arguments += ["--limit", limit]
    if extra:
        extra_file = FOLDER / "ansible-extra.json"
        write_private(extra_file, extra)
        arguments += ["--extra-vars", "@" + str(extra_file)]
    env = environment(PROFILES["foundation"])
    env["ANSIBLE_CONFIG"] = str(ROOT / "infra/ansible/ansible.cfg")
    destination = log_path or FOLDER / f"{playbook}.log"
    try:
        result = subprocess.run(
            arguments,
            cwd=ROOT / "infra/ansible",
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=operation_timeout(1200),
        )
    except subprocess.TimeoutExpired as error:

        def decoded(value):
            return value.decode(errors="replace") if isinstance(value, bytes) else value or ""

        write_private(destination, decoded(error.stdout) + decoded(error.stderr))
        raise RuntimeError(
            f"Ansible {playbook} timed out; inspect private log {destination}"
        ) from None
    write_private(destination, result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"Ansible {playbook} failed; inspect private log {destination}")


def preflight() -> dict:
    Aws(PROFILES["foundation"]).preflight("foundation")
    observation = FoundationObservation.model_validate_json(
        (state_home() / "reports/foundation.json").read_text()
    )
    from verification import verify_aws

    # Refresh live identity/network checks; a cached export alone cannot authorize host changes.
    verify_aws(Aws(PROFILES["foundation"]), observation.allocation.model_dump(mode="json"))
    inventory = yaml.safe_load((state_home() / "reports/ansible-inventory.yaml").read_text())
    if (FOLDER / "inventory.yaml").exists():
        previous = yaml.safe_load((FOLDER / "inventory.yaml").read_text())
        if "external" in previous["all"]["children"]:
            inventory["all"]["children"]["external"] = previous["all"]["children"]["external"]
    recipient = (
        (FOLDER / "backup-recipient.txt").read_text().strip()
        if (FOLDER / "backup-recipient.txt").exists()
        else None
    )
    role_id = Aws(PROFILES["foundation"]).call(
        "iam", "get-role", "--role-name", "klip-control-plane"
    )["Role"]["RoleId"]
    path = write_ansible_inputs(observation.model_dump(mode="json"), inventory, recipient, role_id)
    result = {
        "schema_version": "klip-preflight.v1",
        "captured_at": utc_now(),
        "source_sha256": source_digest(),
        "inventory": str(path),
    }
    write_private(FOLDER / "preflight.json", result)
    return result


def collect_peers() -> dict:
    old_path = FOLDER / "peers.json"
    previous = (
        json.loads(old_path.read_text()) if old_path.exists() else {"revision": 0, "peers": {}}
    )
    peers = dict(previous["peers"])
    for name, fields in load_hosts().items():
        node = fields["klip_node"]
        if node["role"] != "control_plane":
            continue
        key = Host(fields).agent("wg-key")["public_key"]
        observation = json.loads((state_home() / "reports/foundation.json").read_text())
        allocated = observation["allocation"]["nodes"][name]
        peers[name] = {
            key: allocated[key]
            for key in ("instance_id", "public_ip", "private_ip", "wireguard_ip")
        }
        peers[name].update(role="control_plane", public_key=key)
    changed = peers != previous["peers"]
    registry = PeerRegistry(revision=previous["revision"] + int(changed), peers=peers)
    write_private(old_path, registry.model_dump(mode="json"))
    return {"revision": registry.revision, "peers": len(peers)}


def kubeconfigs() -> None:
    cp = Host(load_hosts()["cp-01"])
    write_private(
        FOLDER / "admin.conf", cp.run(["sudo", "cat", "/etc/kubernetes/admin.conf"], secret=True)
    )
    write_private(FOLDER / "ca.crt", cp.run(["sudo", "cat", "/etc/kubernetes/pki/ca.crt"]))
    original = yaml.safe_load((FOLDER / "admin.conf").read_text())
    for entry in original["clusters"]:
        endpoint = entry["cluster"]["server"]
        entry["cluster"].update(
            server="https://127.0.0.1:16443",
            **{"tls-server-name": endpoint.split("://")[1].split(":")[0]},
        )
    write_private(FOLDER / "operator.conf", yaml.safe_dump(original))


def kubectl(*arguments: str, host: str = "cp-01") -> str:
    return Host(load_hosts()[host]).run(
        ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", *arguments]
    )


def bootstrap() -> dict:
    from cluster_verification import verify_wireguard

    hosts = load_hosts()
    verify_wireguard(hosts)
    states = {
        name: Host(fields).agent("state")["state"]
        for name, fields in hosts.items()
        if fields["klip_node"]["role"] == "control_plane"
    }
    if states["cp-01"] == "uninitialized" and any(
        state == "initialized" for state in states.values()
    ):
        raise ValueError(
            "cp-01 is missing from an existing cluster; use member recovery, never init"
        )
    encryption = FOLDER / "encryption.yaml"
    if not encryption.exists():
        initialized = next((name for name, state in states.items() if state == "initialized"), None)
        if initialized:
            write_private(
                encryption,
                Host(hosts[initialized]).run(
                    ["sudo", "cat", "/etc/klip/encryption.yaml"], secret=True
                ),
            )
        else:
            write_private(
                encryption,
                yaml.safe_dump(
                    {
                        "apiVersion": "apiserver.config.k8s.io/v1",
                        "kind": "EncryptionConfiguration",
                        "resources": [
                            {
                                "resources": ["secrets"],
                                "providers": [
                                    {
                                        "aescbc": {
                                            "keys": [
                                                {
                                                    "name": "klip-initial",
                                                    "secret": base64.b64encode(
                                                        os.urandom(32)
                                                    ).decode(),
                                                }
                                            ]
                                        }
                                    },
                                    {"identity": {}},
                                ],
                            }
                        ],
                    }
                ),
            )
    complete = []
    for name in ("cp-01", "cp-02", "cp-03"):
        host = Host(hosts[name])
        config = hosts[name]["klip_node"]
        if states[name] == "uninitialized":
            host.put("/etc/klip/encryption.yaml", encryption.read_text())
            for component, cpu, memory in (
                ("kube-apiserver", "250m", "512Mi"),
                ("kube-controller-manager", "200m", "256Mi"),
                ("kube-scheduler", "100m", "128Mi"),
                ("etcd", "100m", "512Mi"),
            ):
                patch = {
                    "spec": {
                        "containers": [
                            {
                                "name": component,
                                "resources": {"requests": {"cpu": cpu, "memory": memory}},
                            }
                        ]
                    }
                }
                host.put(f"/etc/klip/patches/{component}+strategic.yaml", yaml.safe_dump(patch))
            host.put(
                "/etc/klip/kubeadm-validate.yaml", yaml.safe_dump_all(kubeadm_documents(config))
            )
            host.run(
                [
                    "sudo",
                    "kubeadm",
                    "config",
                    "validate",
                    "--config",
                    "/etc/klip/kubeadm-validate.yaml",
                ]
            )
            host.run(["sudo", "rm", "-f", "/etc/klip/kubeadm-validate.yaml"])
            if name == "cp-01":
                documents = kubeadm_documents(config)
                token = os.urandom(3).hex() + "." + os.urandom(8).hex()
                documents[0]["bootstrapTokens"][0]["token"] = token
                action = "init"
            else:
                primary = Host(hosts["cp-01"])
                certs = (
                    "ca.crt",
                    "ca.key",
                    "sa.key",
                    "sa.pub",
                    "front-proxy-ca.crt",
                    "front-proxy-ca.key",
                    "etcd/ca.crt",
                    "etcd/ca.key",
                )
                for certificate in certs:
                    path = "/etc/kubernetes/pki/" + certificate
                    host.put(path, primary.run(["sudo", "cat", path], secret=True))
                token = primary.run(
                    ["sudo", "kubeadm", "token", "create", "--ttl", "15m"], secret=True
                ).strip()
                ca_hash = primary.run(
                    [
                        "sudo",
                        "sh",
                        "-c",
                        "openssl x509 -pubkey -in /etc/kubernetes/pki/ca.crt "
                        "| openssl pkey -pubin -outform der | sha256sum",
                    ]
                ).split()[0]
                documents = kubeadm_documents(
                    config,
                    join={
                        "apiServerEndpoint": config["api_endpoint"].removeprefix("https://"),
                        "token": token,
                        "caCertHashes": ["sha256:" + ca_hash],
                    },
                )
                action = "join"
            path = "/etc/klip/kubeadm.yaml"
            host.put(path, yaml.safe_dump_all(documents))
            try:
                host.run(["sudo", "kubeadm", action, "--config", path], timeout=900, secret=True)
                if name == "cp-01":
                    host.run(
                        ["sudo", "kubeadm", "token", "delete", token.split(".")[0]], secret=True
                    )
            finally:
                host.run(["sudo", "rm", "-f", path])
                if name != "cp-01":
                    primary.run(
                        ["sudo", "kubeadm", "token", "delete", token.split(".")[0]], secret=True
                    )
        health = host.agent("etcd")
        if len(health["members"]) != len(complete) + 1 and states[name] == "uninitialized":
            raise ValueError("Unexpected etcd membership during sequential bootstrap")
        if not all(item["health"] for item in health["health"]):
            raise ValueError("etcd health failed; further joins stopped")
        complete.append(name)
    kubeconfigs()
    return {"schema_version": "klip-bootstrap.v1", "members": complete, "captured_at": utc_now()}


@contextlib.contextmanager
def tunnel(host: str = "cp-01"):
    hosts = load_hosts()
    node = hosts[host]["klip_node"]
    hostname = node["api_endpoint"].split("://")[1].split(":")[0]
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", 16443))
        except OSError as error:
            raise ValueError("Local API tunnel port 16443 is occupied") from error
    log_path = FOLDER / "tunnel.log"
    write_private(log_path, "")
    with log_path.open("a") as log:
        transport = Host(hosts[host]).ssh_arguments(reuse=False)
        process = subprocess.Popen(
            [
                *transport[:-1],
                "-o",
                "ExitOnForwardFailure=yes",
                "-N",
                "-L",
                f"127.0.0.1:16443:{hostname}:6443",
                transport[-1],
            ],
            env=environment(PROFILES["foundation"]),
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("SSM API tunnel exited; inspect private tunnel log")
                try:
                    verify_ca(FOLDER / "ca.crt", node["api_endpoint"], ("127.0.0.1", 16443))
                    break
                except OSError:
                    time.sleep(1)
            else:
                raise RuntimeError("Timed out verifying API tunnel")
            yield
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def release_arguments(release: str) -> str:
    arguments = "--skip-crds"
    if release == "cilium":
        observation = FoundationObservation.model_validate_json(
            (state_home() / "reports/foundation.json").read_text()
        )
        api = observation.allocation.api
        endpoint = urlparse(str(api.endpoint))
        if (
            endpoint.scheme != "https"
            or endpoint.port != 6443
            or endpoint.hostname != api.dns_name
            or endpoint.username
            or endpoint.password
            or not re.fullmatch(r"[a-z0-9.-]+", endpoint.hostname or "")
        ):
            raise ValueError("Cilium requires the verified private foundation API endpoint")
        arguments += f" --set-string=k8sServiceHost={endpoint.hostname} --set=k8sServicePort=6443"
    return arguments


def deploy() -> dict:
    kubeconfigs()
    with tunnel():
        kubeconfig = str(FOLDER / "operator.conf")
        checked(
            [
                tool("kubectl"),
                "--kubeconfig",
                kubeconfig,
                "apply",
                "--server-side",
                "-f",
                str(ROOT / "artifacts/platform/crds"),
            ]
        )
        for release in ("cilium", "metadata-guard", "metrics-server"):
            if release == "metrics-server":
                for _ in range(30):
                    approve_serving_csrs()
                    pending = json.loads(kubectl("get", "csr", "-o", "json"))["items"]
                    if not any(
                        item["spec"].get("signerName") == "kubernetes.io/kubelet-serving"
                        and not item.get("status", {}).get("certificate")
                        for item in pending
                    ):
                        break
                    time.sleep(2)
                else:
                    raise ValueError("Kubelet serving certificates were not issued")
            checked(
                [
                    tool("helmfile"),
                    "--file",
                    str(ROOT / "deploy/helmfile.yaml"),
                    "--kubeconfig",
                    kubeconfig,
                    "--selector",
                    "name=" + release,
                    "sync",
                    "--args",
                    release_arguments(release),
                ],
                timeout=900,
            )
    kubectl(
        "-n",
        "kube-system",
        "patch",
        "deployment",
        "coredns",
        "--type=merge",
        "-p",
        json.dumps(
            {
                "spec": {
                    "replicas": 2,
                    "template": {
                        "spec": {
                            "tolerations": [
                                {
                                    "key": "node-role.kubernetes.io/control-plane",
                                    "operator": "Exists",
                                    "effect": "NoSchedule",
                                }
                            ],
                            "nodeSelector": {"node-role.kubernetes.io/control-plane": ""},
                            "topologySpreadConstraints": [
                                {
                                    "maxSkew": 1,
                                    "topologyKey": "kubernetes.io/hostname",
                                    "whenUnsatisfiable": "DoNotSchedule",
                                    "labelSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                                }
                            ],
                        }
                    },
                }
            }
        ),
    )
    return {"deployed": True}


def sync_release(release: str) -> dict:
    if release not in {"cilium", "metadata-guard", "metrics-server"}:
        raise ValueError("Only a reviewed static platform release can be reconciled")
    kubeconfigs()
    with tunnel():
        checked(
            [
                tool("helmfile"),
                "--file",
                str(ROOT / "deploy/helmfile.yaml"),
                "--kubeconfig",
                str(FOLDER / "operator.conf"),
                "--selector",
                "name=" + release,
                "sync",
                "--args",
                release_arguments(release),
            ],
            timeout=900,
        )
    return {"release": release, "reconciled": True}


def approve_serving_csrs() -> dict:
    known = {name: fields["klip_node"] for name, fields in load_hosts().items()}
    approved = []
    for csr in json.loads(kubectl("get", "csr", "-o", "json"))["items"]:
        spec = csr["spec"]
        if spec.get("signerName") != "kubernetes.io/kubelet-serving" or csr.get("status", {}).get(
            "conditions"
        ):
            continue
        name = spec["username"].removeprefix("system:node:")
        if name not in known or spec["username"] != "system:node:" + name:
            raise ValueError("Unknown kubelet serving CSR identity")
        request = base64.b64decode(spec["request"]).decode()
        text = checked(["openssl", "req", "-verify", "-noout", "-text"], data=request)
        ips = set(re.findall(r"IP Address:([^,\s]+)", text))
        dns = set(re.findall(r"DNS:([^,\s]+)", text))
        if (
            not ips
            or ips - {known[name]["wireguard_ip"], known[name]["private_ip"]}
            or dns - {name}
        ):
            raise ValueError("Kubelet CSR contains unexpected SANs")
        if (
            not re.search(rf"CN\s*=\s*system:node:{re.escape(name)}(?:[,/\n]|$)", text)
            or not re.search(r"O\s*=\s*system:nodes(?:[,/\n]|$)", text)
            or re.search(r"URI:|email:", text)
            or "client auth" in spec.get("usages", [])
        ):
            raise ValueError("Kubelet CSR subject mismatch")
        live = json.loads(kubectl("get", "node", name, "-o", "json"))
        if not live["spec"].get("providerID", "").endswith("/" + known[name]["instance_id"]):
            raise ValueError("Kubelet CSR provider identity mismatch")
        kubectl("certificate", "approve", csr["metadata"]["name"])
        approved.append(name)
    return {"approved": approved}


def verify() -> dict:
    from cluster_acceptance import remote_manifest

    # A standalone check owns its temporary fixture. Acceptance may supply an
    # existing fixture, which remains the caller's responsibility to remove.
    created = not kubectl(
        "get", "namespace", "klip-acceptance", "--ignore-not-found", "-o", "json"
    ).strip()
    try:
        if created:
            remote_manifest("deploy/releases/smoke.yaml")
        kubectl("-n", "klip-acceptance", "rollout", "status", "deployment/smoke", "--timeout=300s")
        return _verify_cluster_state()
    finally:
        if created:
            kubectl("delete", "namespace", "klip-acceptance", "--wait=true", "--timeout=90s")


def _verify_cluster_state() -> dict:
    from cluster_verification import verify_cluster

    result, nodes = verify_cluster(load_hosts(), kubectl)
    observation = FoundationObservation.model_validate_json(
        (state_home() / "reports/foundation.json").read_text()
    )
    with tunnel():
        ca_ref = verify_ca(
            FOLDER / "ca.crt", str(observation.allocation.api.endpoint), ("127.0.0.1", 16443)
        )
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", 4245))
            except OSError as error:
                raise ValueError("Local Hubble port 4245 is occupied") from error
        env = environment(PROFILES["foundation"])
        log_path = FOLDER / "hubble-forward.log"
        write_private(log_path, "")
        with log_path.open("a") as log:
            process = subprocess.Popen(
                [
                    tool("kubectl"),
                    "--kubeconfig",
                    str(FOLDER / "operator.conf"),
                    "-n",
                    "kube-system",
                    "port-forward",
                    "--address=127.0.0.1",
                    "service/hubble-relay",
                    "4245:80",
                ],
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                for attempt in range(20):
                    try:
                        flows = checked(
                            [
                                tool("hubble"),
                                "observe",
                                "--server=127.0.0.1:4245",
                                "--namespace=klip-acceptance",
                                "--last=10",
                                "--output=json",
                            ],
                            timeout=10,
                        )
                        if not any(json.loads(line).get("flow") for line in flows.splitlines()):
                            raise ValueError("Hubble returned no acceptance flow records")
                        result["hubble_flows"] = len(flows.splitlines())
                        break
                    except RuntimeError:
                        if attempt == 19 or process.poll() is not None:
                            raise
                        time.sleep(1)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    observation = FoundationObservation(
        captured_at=utc_now(),
        allocation=observation.allocation,
        network=observation.network,
        nodes=nodes,
    )
    inventory = shared_inventory(observation, ca_ref, nodes)
    write_private(state_home() / "reports/foundation.json", observation.model_dump(mode="json"))
    write_private(FOLDER / "bootstrap-nodes.json", nodes)
    write_private(
        state_home() / "reports/infrastructure-inventory.json", inventory.model_dump(mode="json")
    )
    write_private(FOLDER / "verification.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "preflight",
            "prepare",
            "collect-peers",
            "mesh",
            "bootstrap",
            "deploy",
            "approve-csrs",
            "verify",
            "backup",
            "backup-check",
            "key-init",
            "key-verify-copy",
            "acceptance",
            "worker-prepare",
            "worker-join",
            "worker-remove",
            "probe-register",
            "probe-cleanup",
            "platform-join",
            "sync-release",
            "verify-platform",
            "activate-identity",
        ],
    )
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--release", choices=["cilium", "metadata-guard", "metrics-server"])
    parser.add_argument("--copy", type=Path)
    parser.add_argument("--confirm-independent-custody", action="store_true")
    args = parser.parse_args()
    private_dir(FOLDER)
    if args.command == "preflight":
        result = preflight()
    elif args.command == "prepare":
        preflight()
        ansible("prepare", limit="control_plane")
        result = collect_peers()
    elif args.command == "collect-peers":
        result = collect_peers()
    elif args.command == "mesh":
        if args.registry:
            registry = PeerRegistry.model_validate_json(args.registry.read_text())
            from node_agent import registry_guard

            current = json.loads((FOLDER / "peers.json").read_text())
            registry_guard(
                registry.model_dump(mode="json"),
                {
                    "revision": current["revision"],
                    "digest": hashlib.sha256(
                        json.dumps(current, sort_keys=True).encode()
                    ).hexdigest(),
                },
            )
            write_private(FOLDER / "peers.json", registry.model_dump(mode="json"))
        ansible("mesh")
        result = {"mesh_reconciled": True}
    elif args.command == "bootstrap":
        result = bootstrap()
    elif args.command == "deploy":
        result = deploy()
    elif args.command == "approve-csrs":
        result = approve_serving_csrs()
    elif args.command == "verify":
        result = verify()
    elif args.command in {"key-init", "key-verify-copy", "backup", "backup-check"}:
        from cluster_backups import operate_backup

        result = operate_backup(
            args.command, args.copy, independent_custody=args.confirm_independent_custody
        )
    elif args.command == "acceptance":
        from cluster_acceptance import acceptance

        result = acceptance()
    elif args.command == "platform-join":
        from platform_workers import join_platform

        result = join_platform()
    elif args.command == "sync-release":
        result = sync_release(args.release)
    elif args.command == "verify-platform":
        from platform_acceptance import verify_platform

        result = verify_platform()
    elif args.command == "activate-identity":
        from issuer_transition import activate

        result = activate()
    else:
        from cluster_workers import operate_worker

        result = operate_worker(args.command, args.request)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        private_dir(FOLDER)
        with (FOLDER / "operator.lock").open("w") as operation_lock:
            fcntl.flock(operation_lock, fcntl.LOCK_EX)
            main()
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        if hasattr(error, "operation_result"):
            print(json.dumps(error.operation_result, indent=2))
        elif len(sys.argv) > 1 and sys.argv[1].startswith("worker-"):
            print(
                json.dumps(
                    {
                        "schema_version": "klip-worker-result.v1",
                        "status": "failed",
                        "cleanup_pending": True,
                        "error": {
                            "code": "invalid_operation",
                            "message": "Invalid worker request or missing executor prerequisites",
                        },
                    }
                )
            )
        else:
            print(f"ERROR: {error}")
        raise SystemExit(1) from None
