"""One permanent Pod/Job owner invokes B's fixed host operations, never a shell payload."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cluster_models import GPUWorkerRequest, PeerRegistry
from common import state_home, utc_now, write_private
from platform_contracts import HostTask, HostTaskObservation

from platform_management.host_executor.controller import canonical
from platform_management.integrations.job_runner import frozen_file


def scope(root):
    document = json.loads((root / "cluster/executor-inputs.json").read_text())
    registry = PeerRegistry.model_validate_json((root / "cluster/peers.json").read_text())
    names = {"cp-01", "cp-02", "cp-03", "platform-01", "platform-02", "platform-03"}
    if (
        document.get("schema_version") != "klip-host-executor-inputs.v1"
        or document.get("cluster_id") != "klip"
        or set(document.get("cpu_peers", {})) != names
        or any(
            registry.model_dump(mode="json")["peers"].get(n) != document["cpu_peers"][n]
            for n in names
        )
    ):
        raise ValueError("Executor scope or protected CPU peer identities differ")
    expected = sorted(
        document["cpu_peers"][n]["public_ip"] + "/32" for n in names if n.startswith("platform-")
    )
    if sorted(document.get("ssh_source_cidrs", [])) != expected:
        raise ValueError("Executor SSH sources must match the three platform host addresses")
    from cluster import Host, load_hosts

    cp = Host(load_hosts()["cp-01"])
    observed = cp.run(
        [
            "sudo",
            "kubectl",
            "--kubeconfig=/etc/kubernetes/admin.conf",
            "get",
            "namespace",
            "kube-system",
            "-o",
            "jsonpath={.metadata.uid}",
        ]
    ).strip()
    if observed != document.get("kube_system_uid"):
        raise ValueError("Trusted executor transport reached a different Kubernetes cluster")
    return document, registry, cp


def access(root, task):
    alias = "credential://klip/lambda-" + task.provider_instance_id
    pins = root / "cluster/known-hosts" / task.provider_instance_id
    frozen_file(pins, (str(task.public_ip) + " " + task.trusted_host_key + "\n").encode())
    fields = {
        "ansible_host": str(task.public_ip),
        "ansible_user": "ubuntu",
        "ansible_ssh_private_key_file": "/run/secrets/host-executor/lambda-ssh-key",
        "ansible_ssh_common_args": "-o StrictHostKeyChecking=yes -o IdentitiesOnly=yes "
        "-o UserKnownHostsFile=" + shlex.quote(str(pins)),
    }
    path = root / "cluster/credential-references.json"
    aliases = json.loads(path.read_text()) if path.exists() else {}
    if alias in aliases and aliases[alias] != fields:
        raise ValueError("Previously pinned host access changed")
    write_private(path, {**aliases, alias: fields})
    return alias


def perform(root, task):
    from cluster_workers import operate_worker

    inputs, registry, cp = scope(root)
    if registry.revision != task.registry_revision:
        raise ValueError("Executor task has a stale peer-registry revision")
    if str(task.public_ip) in {p["public_ip"] for p in inputs["cpu_peers"].values()}:
        raise ValueError("GPU operations cannot target a protected CPU endpoint")
    if task.action in {"cleanup-check", "revoke-bootstrap"}:
        from platform_management.host_executor.cleanup import perform_cleanup

        return perform_cleanup(root, task, registry, cp)
    if task.action == "inspect":
        raise ValueError("GPU/connectivity inspection is not configured; readiness remains false")
    alias = access(root, task)
    request = GPUWorkerRequest(
        operation_id=task.id,
        deadline=task.deadline,
        provider_instance_id=task.provider_instance_id,
        node_name=task.node_name,
        node_uid=task.node_uid,
        public_ip=task.public_ip,
        private_ip=task.private_ip,
        wireguard_ip=task.wireguard_ip,
        access_ref=alias,
        registry_revision=task.registry_revision,
        drain_timeout_s=task.drain_timeout_s,
        ssh_source_cidrs=inputs["ssh_source_cidrs"],
        quarantine=True,
        runtime_profile="nvidia-operator",
    )
    token, bundle = None, None
    try:
        if task.action == "join":
            previous_dir = root / "executions" / task.previous_task_id
            previous = HostTaskObservation.model_validate_json(
                (previous_dir / "observation.json").read_text()
            )
            if previous.status != "completed" or not previous.public_wireguard_key:
                raise ValueError("Join requires the completed host preparation and its public key")
            updated = registry.model_dump(mode="json")
            peer = {
                "instance_id": task.provider_instance_id,
                "role": "worker",
                "public_ip": str(task.public_ip),
                "private_ip": str(task.private_ip),
                "wireguard_ip": str(task.wireguard_ip),
                "public_key": previous.public_wireguard_key,
            }
            old = updated["peers"].get(task.node_name)
            if old is not None and old != peer:
                raise ValueError("Existing GPU peer identity changed")
            if old is None:
                updated["peers"][task.node_name] = peer
                updated["revision"] += 1
                validated = PeerRegistry.model_validate(updated)
                write_private(root / "cluster/peers.json", validated.model_dump(mode="json"))
                request = request.model_copy(update={"registry_revision": validated.revision})
            token = cp.run(
                [
                    "sudo",
                    "kubeadm",
                    "token",
                    "create",
                    "--ttl",
                    "10m",
                    "--description",
                    "klip-" + task.id,
                ],
                secret=True,
            ).strip()
            if not re.fullmatch(r"[a-z0-9]{6}\.[a-z0-9]{16}", token):
                raise ValueError("Join-token issuer returned an invalid credential")
            from cryptography import x509
            from cryptography.hazmat.primitives import serialization

            certificate = x509.load_pem_x509_certificate((root / "cluster/ca.crt").read_bytes())
            public = certificate.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            allocation = json.loads((root / "reports/foundation.json").read_text())["allocation"]
            bundle = Path(tempfile.mkdtemp(prefix="klip-join-")) / "credential.json"
            write_private(
                bundle,
                {
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=9)).isoformat(),
                    "discovery": {
                        "apiServerEndpoint": allocation["api"]["endpoint"]
                        .removeprefix("https://")
                        .rstrip("/"),
                        "token": token,
                        "caCertHashes": ["sha256:" + hashlib.sha256(public).hexdigest()],
                    },
                },
            )
            reference = "credential://klip/join-" + task.id
            aliases_path = root / "cluster/join-references.json"
            aliases = json.loads(aliases_path.read_text()) if aliases_path.exists() else {}
            write_private(aliases_path, {**aliases, reference: str(bundle)})
            request = request.model_copy(update={"join_ref": reference})
        path = root / "executions" / task.id / "effective-request.json"
        frozen_file(path, request.model_dump_json().encode())
        return operate_worker("worker-" + task.action, path)
    finally:
        if bundle:
            bundle.unlink(missing_ok=True)
            bundle.parent.rmdir()
        if token and re.fullmatch(r"[a-z0-9]{6}\.[a-z0-9]{16}", token):
            # Delete only the exact short-lived token created by this execution.
            cp.run(["sudo", "kubeadm", "token", "delete", token.split(".")[0]], secret=True)


def run_task(root, identity, *, action=perform):
    if not re.fullmatch(r"host-[a-f0-9]{24}", identity):
        raise ValueError("Invalid host-task identity")
    directory = root / "executions" / identity
    task = HostTask.model_validate_json((directory / "task.json").read_text())
    receipt = json.loads((directory / "receipt.json").read_text())
    expected = receipt["manifest"]["metadata"]["annotations"]["klip.dev/input-sha256"]
    if task.id != identity or hashlib.sha256(canonical(task)).hexdigest() != expected:
        raise ValueError("Frozen host input differs from its accepted execution")
    pod_uid, job_uid = os.environ.get("POD_UID"), os.environ.get("JOB_UID")
    if not pod_uid or not job_uid:
        raise ValueError("Executor requires actual downward-API Pod and Job identities")
    # Ownership checks precede any mutable output or privileged transport.
    frozen_file(directory / "job-uid", job_uid.encode())
    frozen_file(
        directory / "owner.json",
        json.dumps({"pod_uid": pod_uid, "job_uid": job_uid}, sort_keys=True).encode(),
    )
    result_path = directory / "observation.json"
    if result_path.exists():
        previous = HostTaskObservation.model_validate_json(result_path.read_text())
        if previous.status in {"completed", "failed"}:
            return previous
        raise ValueError("Abandoned execution is not restarted under its original owner")
    common = {
        "task_id": task.id,
        "input_sha256": hashlib.sha256(canonical(task)).hexdigest(),
        "job_uid": job_uid,
        "observed_at": utc_now(),
        "registry_revision": task.registry_revision,
    }
    write_private(
        result_path,
        HostTaskObservation(
            **common, status="running", stage="Executing host operation"
        ).model_dump(mode="json"),
    )
    try:
        with (root / "cluster/operator.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if task.deadline <= datetime.now(UTC):
                raise ValueError("Host execution deadline expired")
            result = action(root, task)
        if (
            result.get("status") != "completed"
            or result["provider_instance_id"] != task.provider_instance_id
        ):
            raise ValueError("Host result does not confirm the requested provider identity")
        checks = result["readiness"]
        observed = HostTaskObservation(
            **{
                **common,
                "observed_at": utc_now(),
                "registry_revision": result["registry_revision"],
            },
            status="completed",
            stage="Host " + task.action + " completed",
            node_name=result.get("node_name") or (task.node_name if task.node_uid else None),
            node_uid=result.get("node_uid") or task.node_uid,
            public_wireguard_key=result.get("public_key"),
            host_ready=checks["host"],
            wireguard_ready=checks["wireguard"],
            kubernetes_ready=checks["kubernetes"],
            cilium_ready=checks["cilium"],
            # Generic B operations cannot establish GPU or Pod connectivity readiness.
            gpu_runtime_ready=False,
            evidence_sha256=result.get("evidence_sha256")
            or hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest(),
            bootstrap_fence_permitted=result.get("bootstrap_fence_permitted", False),
            bootstrap_cleanup_complete=result.get("bootstrap_cleanup_complete", False),
        )
    except Exception as error:
        write_private(
            directory / "private-error.json", {"type": type(error).__name__, "message": str(error)}
        )
        observed = HostTaskObservation(
            **{**common, "observed_at": utc_now()},
            status="failed",
            stage="Host cleanup/reconciliation required",
            cleanup_pending=True,
            error={
                "code": "host_execution_failed",
                "message": "Host operation failed; inspect the executor's private diagnostic",
            },
        )
    write_private(result_path, observed.model_dump(mode="json"))
    return observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    result = run_task(state_home(), parser.parse_args().task)
    raise SystemExit(0 if result.status == "completed" else 1)


if __name__ == "__main__":
    main()
