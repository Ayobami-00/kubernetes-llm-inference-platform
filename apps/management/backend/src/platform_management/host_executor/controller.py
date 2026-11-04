"""Own one fixed Kubernetes Job per typed host task, with durable input/UID receipts."""

import fcntl
import hashlib
import json
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from common import private_dir, utc_now, write_private
from image_release import guard_workloads
from platform_contracts import HostTask, HostTaskObservation
from platform_contracts.host_operations import validate_host_followup

from platform_management.integrations.job_runner import frozen_file
from platform_management.services.common import AppError


def canonical(task):
    document = task.model_dump(mode="json")
    # Preserve v0.1 digests for tasks authored before fenced cleanup was added.
    if document.get("provider_fence") is None:
        document.pop("provider_fence", None)
    return json.dumps(document, sort_keys=True).encode()


class HostController:
    def __init__(self, root: Path, image: str, client, *, protected_ips=()):
        if not re.fullmatch(
            r"127811062476\.dkr\.ecr\.eu-west-2\.amazonaws\.com/klip/management-backend@sha256:[a-f0-9]{64}",
            image,
        ):
            raise ValueError("Host executor requires its reviewed private immutable image")
        self.root, self.image, self.client = root, image, client
        self.protected_ips = frozenset(protected_ips)

    def directory(self, identity):
        if not re.fullmatch(r"host-[a-f0-9]{24}", identity):
            raise ValueError("Invalid host task identity")
        return private_dir(self.root / "executions" / identity)

    @contextmanager
    def lock(self):
        with (private_dir(self.root) / "dispatch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def task(self, identity):
        return HostTask.model_validate_json((self.directory(identity) / "task.json").read_text())

    def manifest(self, task):
        remaining = int((task.deadline - datetime.now(UTC)).total_seconds())
        if not 0 < remaining <= 1200:
            raise AppError(422, "Host task deadline must be within twenty minutes")
        sha = hashlib.sha256(canonical(task)).hexdigest()
        labels = {"app.kubernetes.io/name": "klip-host-executor", "klip.dev/host-task": task.id}
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": task.id,
                "namespace": "platform-ops",
                "labels": labels,
                "annotations": {"klip.dev/input-sha256": sha},
            },
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": remaining,
                "ttlSecondsAfterFinished": 86400,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "restartPolicy": "Never",
                        "serviceAccountName": "klip-host-job",
                        "automountServiceAccountToken": False,
                        "nodeSelector": {
                            "klip.dev.node-restriction.kubernetes.io/platform": "true"
                        },
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 20002,
                            "runAsGroup": 20002,
                            "fsGroup": 20002,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "host-operation",
                                "image": self.image,
                                "command": [
                                    "python",
                                    "-m",
                                    "platform_management.host_executor.worker",
                                    "--task",
                                    task.id,
                                ],
                                "env": [
                                    {"name": name, "value": value}
                                    for name, value in {
                                        "KLIP_STATE_HOME": "/executor/state",
                                        "KLIP_SSH_SOCKET_HOME": "/tmp/klip-ssh",
                                        "PYTHONPATH": "/app/scripts/infrastructure",
                                        "HOME": "/tmp",
                                        "AWS_CONFIG_FILE": "/app/deploy/host-executor-aws-config",
                                        "AWS_EC2_METADATA_DISABLED": "true",
                                        "LANG": "C.UTF-8",
                                        "LC_ALL": "C.UTF-8",
                                        "LANGUAGE": "C",
                                        "PYTHONDONTWRITEBYTECODE": "1",
                                    }.items()
                                ]
                                + [
                                    {"name": name, "valueFrom": {"fieldRef": {"fieldPath": field}}}
                                    for name, field in (
                                        ("POD_UID", "metadata.uid"),
                                        (
                                            "JOB_UID",
                                            "metadata.labels['batch.kubernetes.io/controller-uid']",
                                        ),
                                    )
                                ],
                                "resources": {
                                    "requests": {"cpu": "500m", "memory": "512Mi"},
                                    "limits": {"cpu": "2", "memory": "2Gi"},
                                },
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "volumeMounts": [
                                    {"name": "state", "mountPath": "/executor/state"},
                                    {
                                        "name": "credentials",
                                        "mountPath": "/run/secrets/host-executor",
                                        "readOnly": True,
                                    },
                                    {
                                        "name": "identity",
                                        "mountPath": "/var/run/secrets/klip-host-identity",
                                        "readOnly": True,
                                    },
                                    {"name": "temporary", "mountPath": "/tmp"},
                                    {"name": "ansible", "mountPath": "/app/artifacts/ansible"},
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "state",
                                "persistentVolumeClaim": {"claimName": "klip-host-state"},
                            },
                            {
                                "name": "credentials",
                                "secret": {
                                    "secretName": "klip-host-credentials",
                                    "defaultMode": 0o440,
                                },
                            },
                            {
                                "name": "identity",
                                "projected": {
                                    "defaultMode": 0o440,
                                    "sources": [
                                        {
                                            "serviceAccountToken": {
                                                "audience": "sts.amazonaws.com",
                                                "expirationSeconds": 900,
                                                "path": "token",
                                            }
                                        }
                                    ],
                                },
                            },
                            {"name": "temporary", "emptyDir": {"sizeLimit": "256Mi"}},
                            {"name": "ansible", "emptyDir": {"sizeLimit": "128Mi"}},
                        ],
                    },
                },
            },
        }

    def validate_job(self, job, receipt):
        expected = receipt["manifest"]
        pod = job["spec"]["template"]["spec"]
        if any(
            pod.get(k) for k in ("initContainers", "ephemeralContainers", "shareProcessNamespace")
        ):
            raise AppError(409, "Unexpected executable host Job configuration")
        guard_workloads([job])

        def contains(actual, authored):
            if isinstance(authored, dict):
                return isinstance(actual, dict) and all(
                    k in actual and contains(actual[k], v) for k, v in authored.items()
                )
            if isinstance(authored, list):
                return (
                    isinstance(actual, list)
                    and len(actual) == len(authored)
                    and all(contains(a, b) for a, b in zip(actual, authored, strict=True))
                )
            return actual == authored

        if (
            not contains(job, expected)
            or receipt.get("job_uid", job["metadata"]["uid"]) != job["metadata"]["uid"]
        ):
            raise AppError(409, "Host Job identity or immutable configuration changed")
        uid = job["metadata"]["uid"]
        frozen_file(self.directory(expected["metadata"]["name"]) / "job-uid", uid.encode())
        return uid

    def launch(self, task: HostTask):
        directory = self.directory(task.id)
        frozen_file(directory / "task.json", canonical(task))
        path = directory / "receipt.json"
        with self.lock():
            if path.exists():
                return self.observe(task.id)
            if (
                str(task.public_ip) in self.protected_ips
                or str(task.private_ip) in self.protected_ips
            ):
                raise AppError(409, "GPU host operations cannot target persistent CPU addresses")
            if task.deadline <= datetime.now(UTC):
                return self.observe(task.id)  # Retain an expired input without creating a Job.
            recovery = False
            if task.previous_task_id:
                previous_task = self.task(task.previous_task_id)
                previous_result = self.observe(task.previous_task_id)
                try:
                    recovery = validate_host_followup(task, previous_task, previous_result)
                except ValueError as error:
                    raise AppError(409, str(error)) from error
            active = self.root / "active-task.json"
            if active.exists():
                previous = json.loads(active.read_text())["id"]
                if (
                    previous != task.id
                    and self.task(previous).action == "cleanup-check"
                    and task.previous_task_id != previous
                ):
                    raise AppError(
                        409, "Bootstrap fence/cleanup chain remains reserved", "host_busy"
                    )
                if (
                    previous != task.id
                    and self.observe(previous).status != "completed"
                    and not (recovery and previous == task.previous_task_id)
                ):
                    raise AppError(
                        409,
                        "Previous host operation needs completion or reconciliation",
                        "host_busy",
                    )
            manifest = self.manifest(task)
            write_private(
                active, {"id": task.id, "input_sha256": hashlib.sha256(canonical(task)).hexdigest()}
            )
            receipt = {"manifest": manifest, "submitted_at": utc_now()}
            # Persist uncertainty before I/O. A missing submitted Job is never recreated.
            write_private(path, receipt)
            self.client.request("POST", self.client.job_path(), manifest, conflict=True)
            job = self.client.job(task.id)
            if job:
                receipt["job_uid"] = self.validate_job(job, receipt)
                write_private(path, receipt)
        return self.observe(task.id)

    def observe(self, identity):
        task = self.task(identity)
        directory = self.directory(identity)
        sha = hashlib.sha256(canonical(task)).hexdigest()
        result_path = directory / "observation.json"
        receipt_path = directory / "receipt.json"
        if not receipt_path.exists():
            if task.deadline <= datetime.now(UTC):
                return HostTaskObservation(
                    task_id=identity,
                    input_sha256=sha,
                    observed_at=task.deadline,
                    registry_revision=task.registry_revision,
                    status="failed",
                    stage="Expired before native Job submission",
                    cleanup_pending=True,
                    error={
                        "code": "host_task_expired_unsubmitted",
                        "message": "Expired host input was not submitted; it cannot start",
                    },
                )
            raise AppError(404, "Host task was not submitted")
        receipt = json.loads(receipt_path.read_text())
        if result_path.exists():
            result = HostTaskObservation.model_validate_json(result_path.read_text())
            uid_file = directory / "job-uid"
            if (
                result.task_id != identity
                or result.input_sha256 != sha
                or (receipt.get("job_uid") and result.job_uid != receipt["job_uid"])
                or not uid_file.is_file()
                or result.job_uid != uid_file.read_text()
                or (result.node_name is not None and result.node_name != task.node_name)
                or (
                    task.node_uid is not None
                    and result.node_uid != task.node_uid
                    and (result.status == "completed" or result.node_uid is not None)
                )
                or result.registry_revision < task.registry_revision
            ):
                raise AppError(409, "Host observation differs from its durable task")
            if result.status in {"completed", "failed"}:
                if result.bootstrap_fence_permitted and task.action != "cleanup-check":
                    raise AppError(409, "Unexpected bootstrap fence permission")
                if result.bootstrap_cleanup_complete and task.action != "revoke-bootstrap":
                    raise AppError(409, "Unexpected bootstrap cleanup completion")
                return result  # Terminal idempotence does not need credentials or new API access.
        job = self.client.job(identity)
        common = {
            "task_id": identity,
            "input_sha256": sha,
            "observed_at": utc_now(),
            "registry_revision": task.registry_revision,
        }
        if not job:
            return HostTaskObservation(
                **common,
                status="failed",
                stage="Reconciliation required",
                cleanup_pending=True,
                error={
                    "code": "host_job_lost",
                    "message": "Submitted host Job is missing; it was not restarted",
                },
            )
        uid = self.validate_job(job, receipt)
        conditions = {
            c["type"]
            for c in job.get("status", {}).get("conditions", [])
            if c.get("status") == "True"
        }
        if conditions & {"Complete", "Failed"}:
            finished = job.get("status", {}).get("completionTime") or next(
                (
                    c.get("lastTransitionTime")
                    for c in job.get("status", {}).get("conditions", [])
                    if c.get("status") == "True" and c["type"] == "Failed"
                ),
                None,
            )
            if (
                finished
                and (
                    datetime.now(UTC) - datetime.fromisoformat(finished.replace("Z", "+00:00"))
                ).total_seconds()
                < 120
            ):
                return HostTaskObservation(
                    **common, job_uid=uid, status="running", stage="Finalizing retained artifacts"
                )
            return HostTaskObservation(
                **common,
                job_uid=uid,
                status="failed",
                stage="Reconciliation required",
                cleanup_pending=True,
                error={
                    "code": "host_result_missing",
                    "message": "Terminal host Job has no consistent observation",
                },
            )
        return HostTaskObservation(
            **common, job_uid=uid, status="running", stage="Executing typed host operation"
        )
