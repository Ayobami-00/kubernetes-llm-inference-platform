"""Durable Kubernetes Job transport for execute-only runner commands."""

import hashlib
import json
import os
from pathlib import Path

from platform_contracts import Run, RunResult, RunSnapshot

from platform_management.integrations.cluster_client import ClusterClient
from platform_management.integrations.runner import RunnerObservation
from platform_management.services.common import digest
from platform_management.services.imports import inspect_artifacts


def frozen_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError("Immutable attempt files differ from the frozen execution")
        return
    # Link an fsynced temporary file atomically, without replacing a previous writer.
    import tempfile

    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError("Concurrent attempt staging differs") from None
    finally:
        Path(temporary).unlink(missing_ok=True)


class KubernetesJobRunner:
    def __init__(self, binding, run_id, outcome="passing", client=None):
        self.binding, self.run_id, self.outcome = binding, run_id, outcome
        self.root = Path(binding["artifact_root"]).resolve()
        self.client = client or ClusterClient(binding)

    def close(self):
        self.client.close()

    def directory(self, handle):
        # Handles are management operation slugs, never filesystem paths.
        if not handle.startswith("op-") or any(
            c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in handle
        ):
            raise ValueError("Invalid execution handle")
        path = (self.root / "runs" / self.run_id / handle).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Attempt directory escapes the artifact root")
        return path

    def name(self, handle):
        return "klip-" + handle

    def prepare(self, snapshot, key):
        directory = self.directory(key)
        workload_stub = self.binding["runner"] == "kubernetes-workload-stub:v1"
        if workload_stub and (snapshot.target is None or snapshot.target.mode != "local-stub"):
            raise ValueError("The workload stub adapter requires a frozen local-stub target")
        if self.binding["runner"] == "kubernetes:v1" and (
            snapshot.target is None or snapshot.target.mode != "benchmark"
        ):
            raise ValueError("Real runner execution requires a benchmark target")
        if self.binding["mode"] == "real":
            checks = inspect_artifacts(snapshot.model_dump(mode="json"), self.root)
            if not checks or any(c["status"] != "verified" for c in checks):
                raise ValueError("Execution artifacts must be freshly verified")
        snapshot_bytes = json.dumps(snapshot.model_dump(mode="json"), sort_keys=True).encode()
        snapshot_path = directory / "snapshot.json"
        argv = self.binding["runner_command"]
        fixture = self.binding["runner"] == "kubernetes-fixture:v1"
        if fixture:
            argv = [
                "python",
                "-m",
                "platform_management.integrations.fixture_runner",
                "--snapshot",
                "{snapshot_path}",
                "--output",
                "{attempt_dir}",
                "--outcome",
                self.outcome,
            ]
        replacements = {
            "{snapshot_path}": str(snapshot_path),
            "{attempt_dir}": str(directory),
            "{result_path}": str(directory / "result.json"),
            "{operation_key}": key,
        }
        if (
            not argv
            or not self.binding.get("runner_image")
            or not self.binding.get("artifact_claim")
        ):
            raise ValueError("Runner image, execute command and shared artifact claim are required")
        expanded = []
        for argument in argv:
            for token, value in replacements.items():
                argument = argument.replace(token, value)
            expanded.append(argument)
        manifest = {
            "schema_version": "management-runner.v1",
            "run_id": self.run_id,
            "attempt_id": key,
            "snapshot_path": str(snapshot_path),
            "snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
            "binding_sha256": digest(self.binding),
            "argv": expanded,
            "fixture": fixture or workload_stub,
            "deadline_seconds": snapshot.scenario.warmup.duration_s
            + snapshot.scenario.measurement_duration_s
            + snapshot.scenario.drain_timeout_s
            + self.binding.get("startup_allowance_seconds", 300),
        }
        frozen_file(snapshot_path, snapshot_bytes)
        frozen_file(
            directory / "runner_bridge.py",
            Path(__file__).with_name("runner_bridge.py").read_bytes(),
        )
        frozen_file(directory / "manifest.json", json.dumps(manifest, sort_keys=True).encode())
        return key

    def manifest(self, handle):
        return json.loads((self.directory(handle) / "manifest.json").read_text())

    def labels(self, handle):
        return {
            "app.kubernetes.io/managed-by": "klip",
            "klip.dev/run-id": self.run_id,
            "klip.dev/attempt-id": handle,
        }

    def template(self, handle):
        directory = self.directory(handle)
        manifest = self.manifest(handle)
        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": self.name(handle),
                "labels": self.labels(handle),
                "annotations": {"klip.dev/manifest-sha256": digest(manifest)},
            },
            "spec": {
                "parallelism": 1,
                "completions": 1,
                "backoffLimit": 0,
                "activeDeadlineSeconds": manifest["deadline_seconds"],
                "template": {
                    "metadata": {"labels": self.labels(handle)},
                    "spec": {
                        "restartPolicy": "Never",
                        "terminationGracePeriodSeconds": 40,
                        "serviceAccountName": self.binding.get("service_account", "klip-runner"),
                        "automountServiceAccountToken": False,
                        "nodeSelector": {"node-role.kubernetes.io/control-plane": ""},
                        "tolerations": [
                            {
                                "key": "node-role.kubernetes.io/control-plane",
                                "operator": "Exists",
                                "effect": "NoSchedule",
                            }
                        ],
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 10001,
                            "runAsGroup": 10001,
                            "fsGroup": 10001,
                        },
                        "containers": [
                            {
                                "name": "runner",
                                "image": self.binding["runner_image"],
                                "imagePullPolicy": "IfNotPresent",
                                "command": [
                                    "python",
                                    str(directory / "runner_bridge.py"),
                                    "--manifest",
                                    str(directory / "manifest.json"),
                                ],
                                "env": [
                                    {
                                        "name": "KLIP_POD_UID",
                                        "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
                                    }
                                ],
                                "resources": self.binding.get("runner_resources")
                                or {
                                    "requests": {"cpu": "100m", "memory": "128Mi"},
                                    "limits": {"cpu": "2", "memory": "2Gi"},
                                },
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "volumeMounts": [
                                    {"name": "artifacts", "mountPath": str(self.root)}
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "artifacts",
                                "persistentVolumeClaim": {
                                    "claimName": self.binding["artifact_claim"]
                                },
                            }
                        ],
                    },
                },
            },
        }

        import re

        pod = job["spec"]["template"]["spec"]
        for index, name in enumerate(self.binding.get("runner_secret_refs", [])):
            if not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", name):
                raise ValueError("Runner credential references must be Kubernetes Secret names")
            volume = "credential-" + str(index)
            pod["volumes"].append({"name": volume, "secret": {"secretName": name}})
            pod["containers"][0]["volumeMounts"].append(
                {"name": volume, "mountPath": "/run/secrets/runner/" + name, "readOnly": True}
            )
        return job

    def validate_job(self, job, handle, expected_uid=None):
        meta = job["metadata"]
        if (
            meta.get("labels", {}) != {**meta.get("labels", {}), **self.labels(handle)}
            or meta.get("annotations", {}).get("klip.dev/manifest-sha256")
            != digest(self.manifest(handle))
            or expected_uid
            and meta["uid"] != expected_uid
        ):
            raise ValueError("Job identity or frozen input digest differs; refusing adoption")
        return job

    def ensure_started(self, handle):
        job = self.client.job(self.name(handle))
        if job is None:
            created = self.client.request(
                "POST", self.client.job_path(), self.template(handle), conflict=True
            )
            job = created or self.client.job(self.name(handle))
        if job is None:
            from platform_management.providers.base import TransientProviderError

            raise TransientProviderError("Job creation outcome remains unresolved")
        return self.validate_job(job, handle)["metadata"]["uid"]

    def observe(self, handle, uid):
        job = self.client.job(self.name(handle))
        if not job:
            return RunnerObservation(
                status="failed",
                error={
                    "code": "runner_lost",
                    "message": "The recorded Job is missing; a new run is required",
                },
            )
        self.validate_job(job, handle, uid)
        conditions = {
            c["type"]
            for c in job.get("status", {}).get("conditions", [])
            if c.get("status") == "True"
        }
        if "Failed" in conditions:
            partial = self.saved_observation(handle)
            if partial and partial.result:
                self.validated_result(handle)
            return RunnerObservation(
                status="failed",
                result=partial.result if partial else None,
                timeline_origin_at=partial.timeline_origin_at if partial else None,
                error={
                    "code": "runner_failed",
                    "message": "Runner Job failed or exceeded its deadline",
                },
            )
        if "Complete" in conditions:
            if any(
                p.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
                for p in self.client.pods(self.name(handle))
            ):
                return RunnerObservation(status="running", stage="finalizing")
            try:
                result = self.validated_result(handle)
            except (ValueError, OSError):
                return RunnerObservation(
                    status="failed",
                    error={
                        "code": "invalid_result",
                        "message": "Completed Job has no valid shared RunResult",
                    },
                )
            saved = self.saved_observation(handle)
            return RunnerObservation(
                status="completed",
                result=result,
                timeline_origin_at=saved.timeline_origin_at if saved else None,
            )
        path = self.directory(handle) / "observation.json"
        if path.exists():
            observation = RunnerObservation.model_validate_json(path.read_text())
            if observation.status == "running":
                return observation
        return RunnerObservation(status="running", stage="preparing")

    def validated_result(self, handle):
        directory = self.directory(handle)
        result = RunResult.model_validate_json((directory / "result.json").read_text())
        if self.binding["runner"] != "kubernetes-fixture:v1":
            run = Run.model_validate_json((directory / "run.json").read_text())
            snapshot = RunSnapshot.model_validate_json((directory / "snapshot.json").read_text())
            if (
                run.id != self.run_id
                or run.snapshot != snapshot
                or run.result != result
                or run.fixture != self.manifest(handle)["fixture"]
            ):
                raise ValueError("Runner result disagrees with the frozen management execution")
        return result

    def saved_observation(self, handle):
        path = self.directory(handle) / "observation.json"
        if not path.is_file():
            return None
        return RunnerObservation.model_validate_json(path.read_text())

    def stop(self, handle, uid=None):
        frozen_file(self.directory(handle) / "cancel-requested", b"cancelled")
        job = self.client.job(self.name(handle))
        if job:
            self.validate_job(job, handle, uid)
            self.client.request(
                "DELETE",
                self.client.job_path(self.name(handle)),
                {
                    "propagationPolicy": "Foreground",
                    "preconditions": {"uid": job["metadata"]["uid"]},
                },
                missing=True,
            )
        active = [
            p
            for p in self.client.pods(self.name(handle))
            if p.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
        ]
        return not active and self.client.job(self.name(handle)) is None

    def acknowledge(self, handle, uid):
        job = self.client.job(self.name(handle))
        if job:
            self.validate_job(job, handle, uid)
            self.client.request(
                "PATCH",
                self.client.job_path(self.name(handle)),
                {"spec": {"ttlSecondsAfterFinished": 86400}},
                missing=True,
            )
