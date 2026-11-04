"""Root-only host helper deployed by Ansible. No cloud SDK or controller is required."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

CONFIG = Path("/etc/klip/node.json")
STATE = Path("/var/lib/klip")
UTC = timezone.utc  # noqa: UP017 -- retained Ubuntu 22.04 has Python 3.10


def command(*args: str, input_text: str | None = None, allow_failure: bool = False) -> str:
    result = subprocess.run(args, input=input_text, text=True, capture_output=True, check=False)
    if result.returncode and not (allow_failure and result.stdout.strip()):
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def atomic(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def locked():
    Path("/run/lock").mkdir(exist_ok=True)
    with Path("/run/lock/klip-mesh.lock").open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def cluster_state(paths: set[str], data_present: bool) -> str:
    # The kubelet Debian package keeps its initially empty static-Pod directory.
    # This package marker is not evidence that kubeadm has initialized a cluster.
    paths = paths - {"manifests/.kubelet-keep"}
    required = {"admin.conf", "manifests/kube-apiserver.yaml", "pki/ca.crt"}
    if not paths and not data_present:
        return "uninitialized"
    if required <= paths and data_present:
        return "initialized"
    raise ValueError("Partial bootstrap or retained etcd data: inspect recovery before continuing")


def imds(path: str) -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        "http://169.254.169.254/latest/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
    )
    token = opener.open(request, timeout=3).read().decode()
    request = urllib.request.Request(
        "http://169.254.169.254/latest/" + path, headers={"X-aws-ec2-metadata-token": token}
    )
    return opener.open(request, timeout=3).read().decode()


def validate_host_networks(config: dict, addresses: list[dict]) -> None:
    protected = [
        ipaddress.ip_network(config[key]) for key in ("pod_cidr", "service_cidr", "wireguard_cidr")
    ]
    vpc = ipaddress.ip_network(config["vpc_cidr"])
    if config["role"] != "control_plane" and config["provider"] != "aws":
        protected += [vpc]
    for interface in addresses:
        if interface["ifname"].startswith(("lo", "wg0", "cilium", "lxc", "veth")):
            continue
        for address in interface.get("addr_info", []):
            if address["family"] == "inet":
                actual = ipaddress.ip_network(
                    f"{address['local']}/{address['prefixlen']}", strict=False
                )
                if any(actual.overlaps(network) for network in protected):
                    raise ValueError(f"Host network overlaps cluster allocation: {actual}")
                if actual.overlaps(vpc) and not actual.subnet_of(vpc):
                    raise ValueError("Host network covers more than the project VPC")


def preflight(config: dict) -> dict:
    if config["provider"] == "aws":
        identity = json.loads(imds("dynamic/instance-identity/document"))
        expected_identity = {
            "instanceId": config["instance_id"],
            "accountId": config["account_id"],
            "region": config["region"],
            "privateIp": config["private_ip"],
        }
        if config.get("availability_zone", "external") != "external":
            expected_identity["availabilityZone"] = config["availability_zone"]
        for field, expected in expected_identity.items():
            if identity.get(field) != expected:
                raise ValueError(f"Host identity mismatch: {field}")
    validate_host_networks(config, json.loads(command("ip", "-j", "address", "show")))
    if shutil.disk_usage("/").free < 5 * 1024**3:
        raise ValueError("Root filesystem has less than 5 GiB free")
    if command("timedatectl", "show", "--property=NTPSynchronized", "--value") != "yes":
        raise ValueError("Host clock is not synchronized")
    if Path("/etc/kubernetes/kubelet.conf").exists():
        for binary, version in (
            ("kubelet", config["kubernetes_version"]),
            ("containerd", config["containerd_version"]),
            ("runc", config["runc_version"]),
        ):
            current = command(binary, "--version")
            if not re.search(r"(?<![0-9.])" + re.escape(version) + r"(?![0-9.])", current):
                raise ValueError(f"{binary} differs from the pin; use an explicit upgrade workflow")
    return {"host": True, "instance_id": config["instance_id"]}


def disk_decision(root_device: str, candidate: str, filesystem: str, signatures: str) -> str:
    if root_device == candidate:
        raise ValueError("Refusing to use the root device for etcd")
    if filesystem == "ext4":
        return "reuse"
    if filesystem or signatures.strip():
        raise ValueError("Unrecognized filesystem/signature; never erase existing data")
    return "format-empty"


def storage(config: dict) -> dict:
    volume = config["etcd_volume_id"].replace("-", "")
    candidate = Path("/dev/disk/by-id") / f"nvme-Amazon_Elastic_Block_Store_{volume}"
    if not candidate.exists():
        raise ValueError("Expected EBS disk is absent; refusing root-disk fallback")
    device = str(candidate.resolve())
    root_source = command("findmnt", "-n", "-o", "SOURCE", "/")
    root_parent = command("lsblk", "-no", "PKNAME", root_source).splitlines()[0]
    filesystem = command("lsblk", "-dn", "-o", "FSTYPE", device)
    signatures = command("wipefs", "--no-act", "--noheadings", "--output", "TYPE", device)
    action = disk_decision("/dev/" + root_parent, device, filesystem, signatures)
    if action == "format-empty":
        if Path("/var/lib/etcd").exists() and any(Path("/var/lib/etcd").iterdir()):
            raise ValueError("Unattached existing etcd data requires recovery, not formatting")
        command("mkfs.ext4", "-q", "-L", "klip-etcd", device)
    disk_uuid = command("blkid", "-s", "UUID", "-o", "value", device)
    mount = Path("/var/lib/etcd")
    mount.mkdir(mode=0o700, exist_ok=True)
    mounted = subprocess.run(["mountpoint", "-q", str(mount)], check=False).returncode == 0
    if mounted and command("findmnt", "-n", "-o", "UUID", str(mount)) != disk_uuid:
        raise ValueError("etcd path is mounted from a different volume")
    if not mounted and any(mount.iterdir()):
        raise ValueError("Existing unmounted etcd contents require manual recovery")
    fstab = Path("/etc/fstab")
    lines = [line for line in fstab.read_text().splitlines() if "# klip-etcd" not in line]
    if any(line.split()[1:2] == [str(mount)] for line in lines if not line.startswith("#")):
        raise ValueError("Unmanaged etcd mount exists in fstab")
    lines.append(f"UUID={disk_uuid} {mount} ext4 defaults,nofail 0 2 # klip-etcd")
    atomic(fstab, "\n".join(lines) + "\n", 0o644)
    if not mounted:
        command("mount", str(mount))
    marker = mount / ".klip-volume.json"
    if marker.exists():
        if json.loads(marker.read_text()) != {
            "volume_id": config["etcd_volume_id"],
            "cluster_id": "klip",
        }:
            raise ValueError("etcd volume ownership marker disagrees with allocation")
    elif any(item.name != "lost+found" for item in mount.iterdir()):
        raise ValueError("Existing unowned etcd volume contents require manual recovery")
    else:
        atomic(marker, json.dumps({"volume_id": config["etcd_volume_id"], "cluster_id": "klip"}))
    return {"storage": True, "action": action, "uuid": disk_uuid}


def wg_key() -> str:
    key = Path("/etc/wireguard/klip.key")
    if not key.exists():
        if Path("/etc/wireguard/wg0.conf").exists():
            raise ValueError("Existing mesh has lost its key; use explicit key recovery/rotation")
        atomic(key, command("wg", "genkey") + "\n")
    return command("wg", "pubkey", input_text=key.read_text())


def gateway_decision(state: dict, healthy: dict[str, bool], now: float) -> tuple[dict, bool]:
    result = dict(state)
    active = state.get("active")
    failures = 0 if healthy.get(active, False) else state.get("failures", 0) + 1
    result["failures"] = failures
    if active and (failures < 3 or now - state.get("switched_at", 0) < 30):
        return result, False
    candidates = sorted(name for name, good in healthy.items() if good)
    if not candidates:
        return result, False
    selected = candidates[0]
    result.update(active=selected, failures=0, switched_at=now)
    return result, selected != active


def apply_mesh(config: dict, registry: dict, state: dict) -> None:
    own = registry["peers"][config["name"]]
    lines = [
        "[Interface]",
        f"PrivateKey = {Path('/etc/wireguard/klip.key').read_text().strip()}",
        f"Address = {own['wireguard_ip']}/24",
        "ListenPort = 51820",
        "MTU = 1420",
    ]
    for name, peer in sorted(registry["peers"].items()):
        if name == config["name"]:
            continue
        allowed = [peer["wireguard_ip"] + "/32"]
        if name == state.get("active") and own["role"] != "control_plane":
            allowed += config["api_routed_cidrs"]
        endpoint = (
            peer["private_ip"]
            if own["role"] == peer["role"] == "control_plane"
            else peer["public_ip"]
        )
        lines += [
            "",
            "[Peer]",
            f"PublicKey = {peer['public_key']}",
            f"AllowedIPs = {', '.join(allowed)}",
            f"Endpoint = {endpoint}:51820",
            "PersistentKeepalive = 25",
        ]
    path = Path("/etc/wireguard/wg0.conf")
    atomic(path, "\n".join(lines) + "\n")
    if subprocess.run(["ip", "link", "show", "wg0"], capture_output=True, check=False).returncode:
        command("wg-quick", "up", "wg0")
    else:
        stripped = command("wg-quick", "strip", "wg0")
        fd, temporary = tempfile.mkstemp(dir="/run")
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(stripped)
            command("wg", "syncconf", "wg0", temporary)
        finally:
            Path(temporary).unlink(missing_ok=True)
    interfaces = json.loads(command("ip", "-j", "address", "show", "dev", "wg0"))
    for address in interfaces[0].get("addr_info", []):
        if address["family"] == "inet" and (
            address["local"] != own["wireguard_ip"] or address["prefixlen"] != 24
        ):
            command(
                "ip", "address", "del", f"{address['local']}/{address['prefixlen']}", "dev", "wg0"
            )
    # Removing an old primary can also remove Linux's secondary addresses;
    # establish the authoritative assignment after removing obsolete addresses.
    command("ip", "address", "replace", own["wireguard_ip"] + "/24", "dev", "wg0")
    command("ip", "link", "set", "wg0", "mtu", "1420")
    for cidr in (
        config["api_routed_cidrs"] if own["role"] != "control_plane" and state.get("active") else []
    ):
        command("ip", "route", "replace", cidr, "dev", "wg0", "src", own["wireguard_ip"])


def mesh(config: dict) -> dict:
    registry = json.loads(Path("/etc/klip/peers.next.json").read_text())
    state_path = STATE / "mesh.json"
    with locked():
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        digest = registry_guard(registry, state)
        if state.get("active") not in registry["peers"]:
            state.pop("active", None)
        apply_mesh(config, registry, state)
        atomic(Path("/etc/klip/peers.json"), json.dumps(registry))
        state.update(revision=registry["revision"], digest=digest)
        atomic(state_path, json.dumps(state))
    return {"mesh_configured": True, "revision": registry["revision"]}


def registry_guard(registry: dict, state: dict) -> str:
    if registry["revision"] < state.get("revision", 0):
        raise ValueError("Stale mesh revision")
    digest = hashlib.sha256(json.dumps(registry, sort_keys=True).encode()).hexdigest()
    if registry["revision"] == state.get("revision") and digest != state.get("digest"):
        raise ValueError("Conflicting mesh contents at the same revision")
    return digest


def apply_firewall() -> dict:
    command("iptables-restore", "--noflush", "/etc/klip/firewall.rules")
    for table, parent, child in (
        ("filter", "INPUT", "KLIP-HOST"),
        ("filter", "FORWARD", "KLIP-FORWARD"),
        ("nat", "POSTROUTING", "KLIP-API-SNAT"),
    ):
        check = subprocess.run(
            ["iptables", "-t", table, "-C", parent, "-j", child], capture_output=True, check=False
        )
        if check.returncode:
            command("iptables", "-t", table, "-I", parent, "1", "-j", child)
    return {"firewall": True}


def route_tick(config: dict) -> dict:
    with locked():
        registry = json.loads(Path("/etc/klip/peers.json").read_text())
        path = STATE / "mesh.json"
        state = json.loads(path.read_text())
        healthy = {}
        context = ssl.create_default_context(cafile="/etc/klip/ca.crt")
        for name, peer in registry["peers"].items():
            if peer["role"] != "control_plane":
                continue
            try:
                with socket.create_connection((peer["wireguard_ip"], 6443), timeout=1) as sock:
                    with context.wrap_socket(sock, server_hostname=peer["wireguard_ip"]) as tls:
                        tls.sendall(b"GET /livez HTTP/1.0\r\nHost: kubernetes\r\n\r\n")
                        healthy[name] = b"200" in tls.recv(256).split(b"\r\n")[0]
            except OSError:
                healthy[name] = False
        state, changed = gateway_decision(state, healthy, time.time())
        if changed:
            apply_mesh(config, registry, state)
        atomic(path, json.dumps(state))
        return {"active": state.get("active"), "healthy": healthy, "changed": changed}


def etcd(*args: str, allow_failure: bool = False) -> str:
    endpoint = "--runtime-endpoint=unix:///run/containerd/containerd.sock"
    containers = command("crictl", endpoint, "ps", "--name", "etcd", "-q").splitlines()
    if len(containers) != 1:
        raise ValueError("Expected one running etcd container")
    return command(
        "crictl",
        endpoint,
        "exec",
        containers[0],
        "etcdctl",
        "--endpoints=https://127.0.0.1:2379",
        "--cacert=/etc/kubernetes/pki/etcd/ca.crt",
        "--cert=/etc/kubernetes/pki/etcd/healthcheck-client.crt",
        "--key=/etc/kubernetes/pki/etcd/healthcheck-client.key",
        *args,
        allow_failure=allow_failure,
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def upload(config: dict, key: str, path: Path) -> dict:
    role = imds("meta-data/iam/security-credentials/").strip()
    credentials = json.loads(imds("meta-data/iam/security-credentials/" + role))
    now = datetime.now(UTC)
    stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    region = config["region"]
    host = f"{config['backup_bucket']}.s3.{region}.amazonaws.com"
    uri = "/" + urllib.parse.quote(key, safe="/-_.~")
    digest = file_sha256(path)
    headers = {
        "host": host,
        "x-amz-content-sha256": digest,
        "x-amz-date": stamp,
        "x-amz-security-token": credentials["Token"],
        "x-amz-server-side-encryption": "AES256",
    }
    signed = ";".join(sorted(headers))
    canonical = (
        "PUT\n"
        + uri
        + "\n\n"
        + "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
        + "\n"
        + signed
        + "\n"
        + digest
    )
    scope = f"{day}/{region}/s3/aws4_request"
    message = (
        "AWS4-HMAC-SHA256\n"
        + stamp
        + "\n"
        + scope
        + "\n"
        + hashlib.sha256(canonical.encode()).hexdigest()
    )
    secret = ("AWS4" + credentials["SecretAccessKey"]).encode()
    for part in (day, region, "s3", "aws4_request"):
        secret = hmac.new(secret, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={credentials['AccessKeyId']}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    headers["Content-Length"] = str(path.stat().st_size)
    with path.open("rb") as stream:
        request = urllib.request.Request(
            f"https://{host}{uri}", method="PUT", data=stream, headers=headers
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            version = response.headers.get("x-amz-version-id")
            if not version:
                raise ValueError("Recovery upload returned no S3 object version")
            return {"version_id": version, "sha256": digest, "key": key}


def backup(config: dict) -> dict:
    preflight(config)
    if not Path("/var/lib/etcd/data/member").is_dir() or not os.path.ismount("/var/lib/etcd"):
        raise ValueError("A mounted, initialized etcd volume is required")
    identifier = str(uuid.uuid4())
    snapshot = Path("/var/lib/etcd/data") / f"klip-snapshot-{identifier}.db"
    try:
        etcd("snapshot", "save", str(snapshot))
        with tempfile.TemporaryDirectory(prefix="klip-backup-", dir="/var/lib/klip") as directory:
            folder = Path(directory)
            archive = folder / "recovery.tar.gz"
            metadata = {
                "schema_version": "klip-recovery.v1",
                "cluster_id": "klip",
                "instance_id": config["instance_id"],
                "captured_at": datetime.now(UTC).isoformat(),
                "snapshot_sha256": file_sha256(snapshot),
                "kubernetes_version": config["kubernetes_version"],
            }
            (folder / "metadata.json").write_text(json.dumps(metadata))
            with tarfile.open(archive, "w:gz") as tar:
                for source, name in (
                    (snapshot, "snapshot.db"),
                    (Path("/etc/kubernetes/pki"), "pki"),
                    (Path("/etc/klip/encryption.yaml"), "encryption.yaml"),
                    (folder / "metadata.json", "metadata.json"),
                ):
                    tar.add(source, arcname=name)
            encrypted = folder / "recovery.age"
            command("age", "-r", config["backup_recipient"], "-o", str(encrypted), str(archive))
            result = upload(
                config,
                f"clusters/klip/{config['backup_principal_id']}/{identifier}.tar.gz.age",
                encrypted,
            )
            result.update(metadata)
            atomic(STATE / "backup.json", json.dumps(result))
            return result
    finally:
        snapshot.unlink(missing_ok=True)


def validate_pod_mtu(links: list[dict], routes: list[dict]) -> dict:
    # Cilium keeps the veth at the base MTU and subtracts encapsulation overhead
    # from the route MTU. Checking only eth0 misses the effective packet limit.
    defaults = [route for route in routes if route.get("dst") == "default"]
    if len(links) != 1 or links[0]["mtu"] != 1420:
        raise ValueError("Pod interface MTU must match the WG1420 base")
    if len(defaults) != 1 or defaults[0].get("metrics") != [{"mtu": 1370}]:
        raise ValueError("Pod default route must use MTU1370 for VXLAN over WG1420")
    return {"pod_interface_mtu": 1420, "pod_route_mtu": 1370}


def pod_network(
    pod_uid: str | None = None, *, cri_socket: str = "unix:///run/containerd/containerd.sock"
) -> dict:
    if cri_socket not in {
        "unix:///run/containerd/containerd.sock",
        "unix:///run/klip-containerd/containerd.sock",
    }:
        raise ValueError("Unreviewed CRI socket for Pod-network inspection")
    endpoint = "--runtime-endpoint=" + cri_socket
    if pod_uid is not None:
        if not re.fullmatch(r"[a-f0-9-]{36}", pod_uid):
            raise ValueError("Invalid Pod-network sandbox UID")
        observed = json.loads(command("crictl", endpoint, "pods", "-o", "json"))
        pods = [
            p["id"]
            for p in observed["items"]
            if p.get("metadata", {}).get("uid") == pod_uid and p.get("state") == "SANDBOX_READY"
        ]
        if len(pods) != 1:
            raise ValueError("Expected exactly one Ready sandbox with the admitted Pod UID")
    else:
        pods = command("crictl", endpoint, "pods", "--name", "smoke", "-q").splitlines()
    if not pods:
        raise ValueError("No smoke Pod sandbox exists on this host")
    sandbox = json.loads(command("crictl", endpoint, "inspectp", pods[0]))
    if pod_uid is not None and sandbox.get("status", {}).get("metadata", {}).get("uid") != pod_uid:
        raise ValueError("Pod sandbox identity changed during MTU inspection")
    pid = sandbox["info"]["pid"]
    links = json.loads(command("nsenter", "-t", str(pid), "-n", "ip", "-j", "link", "show", "eth0"))
    routes = json.loads(command("nsenter", "-t", str(pid), "-n", "ip", "-j", "route", "show"))
    return validate_pod_mtu(links, routes)


def main() -> None:
    def interrupted(signum, frame):
        raise InterruptedError("Host operation interrupted; cleaning private temporary artifacts")

    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "preflight",
            "storage",
            "wg-key",
            "mesh",
            "route",
            "state",
            "etcd",
            "backup",
            "status",
            "firewall",
            "pod-network",
        ],
    )
    parser.add_argument("--pod-uid")
    args = parser.parse_args()
    if args.pod_uid is not None and args.action != "pod-network":
        parser.error("--pod-uid is accepted only for pod-network")
    config = json.loads(CONFIG.read_text())
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.action == "pod-network":
        result = pod_network(
            args.pod_uid,
            cri_socket=config.get("cri_socket", "unix:///run/containerd/containerd.sock"),
        )
    elif args.action == "firewall":
        result = apply_firewall()
    elif args.action == "wg-key":
        result = {"public_key": wg_key()}
    elif args.action == "state":
        root = Path("/etc/kubernetes")
        paths = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
        # Kubelet client files on workers are intentionally handled separately.
        if config["role"] != "control_plane":
            result = {
                "state": "initialized" if (root / "kubelet.conf").exists() else "uninitialized"
            }
        else:
            result = {"state": cluster_state(paths, Path("/var/lib/etcd/data/member").exists())}
    elif args.action == "etcd":
        result = {
            "health": json.loads(
                etcd("endpoint", "health", "--cluster", "-w", "json", allow_failure=True)
            ),
            "members": json.loads(etcd("member", "list", "-w", "json"))["members"],
        }
    elif args.action == "status":
        result = {
            "host": preflight(config),
            "wireguard": command("wg", "show", "wg0", "latest-handshakes"),
            "mesh": json.loads((STATE / "mesh.json").read_text()),
            "backup": json.loads((STATE / "backup.json").read_text())
            if (STATE / "backup.json").exists()
            else None,
        }
    else:
        result = {
            "preflight": preflight,
            "storage": storage,
            "mesh": mesh,
            "route": route_tick,
            "backup": backup,
        }[args.action](config)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
