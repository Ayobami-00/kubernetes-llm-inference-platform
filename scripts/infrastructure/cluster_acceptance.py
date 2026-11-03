"""Bounded live acceptance with failure cleanup; never uses prebootstrap HTTP listeners."""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime, timedelta

import yaml
from common import PROFILES, ROOT, Aws, state_home, tf, utc_now, write_private


def wait_for(check, timeout: int = 300):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (RuntimeError, OSError) as error:
            last = error
        time.sleep(3)
    raise RuntimeError(f"Acceptance wait timed out: {last}")


def remote_manifest(path: str) -> None:
    from cluster import Host, kubectl, load_hosts

    host = Host(load_hosts()["cp-01"])
    remote = "/etc/klip/" + path.rsplit("/", 1)[-1]
    host.put(remote, (ROOT / path).read_text())
    kubectl("apply", "-f", remote)


def probe_credentials() -> None:
    from cluster import FOLDER, Host, kubectl, load_hosts

    remote_manifest("deploy/releases/probe-access.yaml")
    token = kubectl(
        "-n", "klip-acceptance", "create", "token", "network-probe", "--duration=20m"
    ).strip()
    observation = json.loads((state_home() / "reports/foundation.json").read_text())
    kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "probe",
        "clusters": [
            {
                "name": "klip",
                "cluster": {
                    "server": observation["allocation"]["api"]["endpoint"],
                    "certificate-authority-data": base64.b64encode(
                        (FOLDER / "ca.crt").read_bytes()
                    ).decode(),
                },
            }
        ],
        "users": [{"name": "probe", "user": {"token": token}}],
        "contexts": [
            {
                "name": "probe",
                "context": {"cluster": "klip", "user": "probe", "namespace": "klip-acceptance"},
            }
        ],
    }
    Host(load_hosts()["network-probe"]).put("/etc/klip/probe.conf", yaml.safe_dump(kubeconfig))
    kubectl("-n", "klip-acceptance", "delete", "configmap", "quorum-probe", "--ignore-not-found")
    kubectl(
        "-n",
        "klip-acceptance",
        "create",
        "configmap",
        "quorum-probe",
        "--from-literal=sequence=initial",
    )


def write_canary() -> bool:
    from cluster import Host, load_hosts

    Host(load_hosts()["network-probe"]).run(
        [
            "sudo",
            "kubectl",
            "--kubeconfig=/etc/klip/probe.conf",
            "--request-timeout=3s",
            "patch",
            "configmap",
            "quorum-probe",
            "--type=merge",
            "-p",
            json.dumps({"data": {"sequence": utc_now()}}),
        ],
        timeout=20,
        secret=True,
    )
    return True


def worker_probe() -> dict:
    from cluster import FOLDER, Host, load_hosts
    from cluster_models import PeerRegistry
    from cluster_workers import operate_worker

    fields = load_hosts()["network-probe"]
    config = fields["klip_node"]
    aliases = FOLDER / "credential-references.json"
    references = json.loads(aliases.read_text()) if aliases.exists() else {}
    references["credential://klip/probe-ssh"] = {
        key: value for key, value in fields.items() if key.startswith("ansible_")
    }
    write_private(aliases, references)
    revision = json.loads((FOLDER / "peers.json").read_text())["revision"]
    request = {
        "schema_version": "klip-worker-operation.v1",
        "cluster_id": "klip",
        "operation_id": "probe-prepare-" + str(int(time.time())),
        "deadline": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        "provider": "aws",
        "provider_instance_id": config["instance_id"],
        "node_name": "network-probe",
        "public_ip": json.loads((FOLDER / "peers.json").read_text())["peers"]["network-probe"][
            "public_ip"
        ],
        "private_ip": config["private_ip"],
        "wireguard_ip": "10.200.0.64",
        "access_ref": "credential://klip/probe-ssh",
        "registry_revision": revision,
    }
    path = FOLDER / "probe-worker-request.json"
    write_private(path, request)
    prepared = operate_worker("worker-prepare", path)
    registry = PeerRegistry.model_validate_json((FOLDER / "peers.json").read_text()).model_dump(
        mode="json"
    )
    registry["revision"] += 1
    registry["peers"]["network-probe"].update(
        role="worker", wireguard_ip="10.200.0.64", public_key=prepared["public_key"]
    )
    write_private(
        FOLDER / "peers.json", PeerRegistry.model_validate(registry).model_dump(mode="json")
    )
    token = (
        Host(load_hosts()["cp-01"])
        .run(["sudo", "kubeadm", "token", "create", "--ttl", "15m"], secret=True)
        .strip()
    )
    ca_hash = (
        Host(load_hosts()["cp-01"])
        .run(
            [
                "sudo",
                "sh",
                "-c",
                "openssl x509 -pubkey -in /etc/kubernetes/pki/ca.crt "
                "| openssl pkey -pubin -outform der | sha256sum",
            ]
        )
        .split()[0]
    )
    credential = FOLDER / "probe-join-bundle.json"
    write_private(
        credential,
        {
            "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            "discovery": {
                "apiServerEndpoint": config["api_endpoint"].removeprefix("https://"),
                "token": token,
                "caCertHashes": ["sha256:" + ca_hash],
            },
        },
    )
    joins = FOLDER / "join-references.json"
    join_refs = json.loads(joins.read_text()) if joins.exists() else {}
    join_refs["credential://klip/probe-join"] = str(credential)
    write_private(joins, join_refs)
    request.update(
        operation_id="probe-join-" + str(int(time.time())),
        registry_revision=registry["revision"],
        join_ref="credential://klip/probe-join",
    )
    write_private(path, request)
    try:
        joined = operate_worker("worker-join", path)
        request.update(
            operation_id="probe-remove-" + str(int(time.time())),
            node_uid=joined["node_uid"],
            registry_revision=joined["registry_revision"],
        )
        write_private(path, request)
        removed = operate_worker("worker-remove", path)
        return {
            "joined": joined["node_uid"],
            "removed": removed["status"] == "completed",
            "gpu_runtime": False,
        }
    finally:
        credential.unlink(missing_ok=True)
        Host(load_hosts()["cp-01"]).run(
            ["sudo", "kubeadm", "token", "delete", token.split(".")[0]], secret=True
        )


def acceptance() -> dict:
    from cluster import FOLDER, Host, bootstrap, kubectl, load_hosts, verify
    from cluster_backups import operate_backup
    from cluster_workers import operate_worker
    from manage import apply_plan, create_plan

    aws = Aws(PROFILES["foundation"])
    aws.preflight("foundation")
    hosts = load_hosts()
    stopped = hosts["cp-01"]["klip_node"]["instance_id"]
    stopped_name = "cp-01"
    report = {
        "schema_version": "klip-acceptance.v1",
        "started_at": utc_now(),
        "samples": [],
        "cleanup_errors": [],
    }
    try:
        remote_manifest("deploy/releases/smoke.yaml")
        kubectl("-n", "klip-acceptance", "rollout", "status", "deployment/smoke", "--timeout=300s")
        verify()
        operate_worker("probe-register", None)
        operate_backup("backup")
        report["backup"] = operate_backup("backup-check")
        probe_credentials()
        write_canary()
        active = Host(load_hosts()["network-probe"]).agent("status")["mesh"].get("active")
        if active not in {"cp-01", "cp-02", "cp-03"}:
            raise ValueError("Probe has no known healthy CPU gateway")
        stopped = hosts[active]["klip_node"]["instance_id"]
        stopped_name = active
        report["stopped_gateway"] = active
        aws.call("ec2", "stop-instances", "--instance-ids", stopped)
        start = time.monotonic()
        recovered = False
        while time.monotonic() - start < 90:
            sample = {"offset_s": round(time.monotonic() - start, 2)}
            try:
                write_canary()
                gateway = Host(load_hosts()["network-probe"]).agent("status")["mesh"].get("active")
                sample.update(success=True, gateway=gateway)
                allocation = json.loads((state_home() / "reports/foundation.json").read_text())[
                    "allocation"
                ]
                targets = aws.call(
                    "elbv2",
                    "describe-target-health",
                    "--target-group-arn",
                    allocation["api"]["target_group_arn"],
                )["TargetHealthDescriptions"]
                sample["target_states"] = {
                    item["Target"]["Id"]: item["TargetHealth"]["State"] for item in targets
                }
                if (
                    gateway
                    and gateway != active
                    and sample["target_states"][hosts[active]["klip_node"]["private_ip"]]
                    == "unhealthy"
                ):
                    recovered = True
            except (RuntimeError, OSError):
                sample["success"] = False
            report["samples"].append(sample)
            if recovered:
                break
            time.sleep(2)
        if not recovered:
            raise ValueError("Private API gateway recovery exceeded 90 seconds")
        for _ in range(10):
            write_canary()
        survivor = next(name for name in sorted(hosts) if name.startswith("cp-") and name != active)
        health = Host(hosts[survivor]).agent("etcd")
        if sum(bool(item["health"]) for item in health["health"]) < 2:
            raise ValueError("Surviving etcd quorum could not be verified")
        aws.call("ec2", "start-instances", "--instance-ids", stopped)
        wait_for(lambda: Host(hosts[active]).agent("preflight"))
        wait_for(
            lambda: (
                len(Host(hosts["cp-01"]).agent("etcd")["health"]) == 3
                and all(item["health"] for item in Host(hosts["cp-01"]).agent("etcd")["health"])
            )
        )
        report["worker"] = worker_probe()
        # Rerunning bootstrap must retain the same CA and etcd member identities.
        before = Host(hosts["cp-01"]).agent("etcd")["members"]
        ca_before = (FOLDER / "ca.crt").read_bytes()
        bootstrap()
        if (
            before != Host(hosts["cp-01"]).agent("etcd")["members"]
            or ca_before != (FOLDER / "ca.crt").read_bytes()
        ):
            raise ValueError("Bootstrap rerun changed cluster identity")
        report["status"] = "passed"
    finally:
        try:
            aws.call("ec2", "start-instances", "--instance-ids", stopped)
            wait_for(lambda: Host(hosts[stopped_name]).agent("preflight"))
        except Exception as error:
            report["cleanup_errors"].append(f"CPU recovery: {error}")
        try:
            # Revoke the short-lived probe service account even if a test failed.
            kubectl(
                "-n",
                "klip-acceptance",
                "delete",
                "serviceaccount",
                "network-probe",
                "--ignore-not-found",
            )
            kubectl(
                "-n",
                "klip-acceptance",
                "delete",
                "role/network-probe",
                "rolebinding/network-probe",
                "configmap/quorum-probe",
                "--ignore-not-found",
            )
            current = kubectl("get", "node", "network-probe", "--ignore-not-found", "-o", "json")
            if current.strip():
                raise ValueError(
                    "Probe worker remains joined; complete UID-guarded removal before destroying it"
                )
            operate_worker("probe-cleanup", None)
            report["probe_instance_id"] = json.loads(tf("probe", "output", "-json", "probe"))[
                "instance_id"
            ]
            write_private(
                FOLDER / "probe-cleanup.json",
                {
                    "cluster_id": "klip",
                    "probe_instance_id": report["probe_instance_id"],
                    "node_absent": True,
                    "peers_revoked": True,
                },
            )
            plan = create_plan(
                "probe",
                phase="destroy",
                teardown=json.loads((FOLDER / "probe-cleanup.json").read_text()),
            )
            apply_plan("probe", plan["plan_sha256"], teardown=True)
        except Exception as error:
            report["cleanup_errors"].append(f"Probe cleanup: {error}")
        report["finished_at"] = utc_now()
        write_private(FOLDER / "acceptance.json", report)
    if report["cleanup_errors"]:
        raise ValueError("Acceptance cleanup needs attention; inspect private acceptance report")
    verify()
    return report
