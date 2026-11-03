"""Real RWX writes, cross-worker reads and cleanup on the retained artifact claim."""

from __future__ import annotations

import json
import re
import uuid

from common import source_digest, utc_now, write_private
from federation_probe import probe_image
from smoke import wait_until


def job_document(operation: str, node: str, stage: str) -> dict:
    if (
        not re.fullmatch(r"[a-f0-9]{12}", operation)
        or node not in {"platform-01", "platform-02", "platform-03"}
        or stage not in {"write", "read", "cleanup"}
    ):
        raise ValueError("Unexpected storage-canary identity")
    script = """
set -eu
base=/artifacts/.verification-$KLIP_OPERATION
case "$KLIP_STAGE" in
write)
  umask 077
  mkdir -p "$base"
  test "$(stat -c %u /artifacts)" = 10001
  test "$(stat -c %g /artifacts)" = 10001
  printf '%s\n' "$KLIP_OPERATION:$KLIP_NODE" > "$base/$KLIP_NODE"
  sync -f "$base/$KLIP_NODE"
  ;;
read)
  for node in platform-01 platform-02 platform-03; do
    test "$(cat "$base/$node")" = "$KLIP_OPERATION:$node"
  done
  ;;
cleanup)
  for node in platform-01 platform-02 platform-03; do
    test "$(cat "$base/$node")" = "$KLIP_OPERATION:$node"
  done
  rm "$base/platform-01" "$base/platform-02" "$base/platform-03"
  rmdir "$base"
  ;;
esac
printf 'KLIP_STORAGE_%s_OK\n' "$KLIP_STAGE"
"""
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"storage-{stage}-{node}-{operation}",
            "namespace": "klip",
            "annotations": {"klip.dev/verification-id": operation},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 180,
            "ttlSecondsAfterFinished": 600,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "nodeSelector": {
                        "klip.dev.node-restriction.kubernetes.io/platform": "true",
                        "kubernetes.io/hostname": node,
                    },
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "probe",
                            "image": probe_image(),
                            "command": ["/bin/sh", "-ec", script],
                            "env": [
                                {"name": "KLIP_OPERATION", "value": operation},
                                {"name": "KLIP_NODE", "value": node},
                                {"name": "KLIP_STAGE", "value": stage},
                                {"name": "AWS_EC2_METADATA_DISABLED", "value": "true"},
                            ],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "64Mi"},
                                "limits": {"cpu": "500m", "memory": "256Mi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [{"name": "artifacts", "mountPath": "/artifacts"}],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "artifacts",
                            "persistentVolumeClaim": {"claimName": "klip-artifacts"},
                        }
                    ],
                }
            },
        },
    }


def verify() -> dict:
    from cluster import FOLDER, Host, delete_owned_resource, kubectl, load_hosts

    operation = uuid.uuid4().hex[:12]
    report = {
        "schema_version": "klip-storage-canary.v1",
        "operation_id": operation,
        "status": "running",
        "started_at": utc_now(),
        "source_sha256": source_digest(),
        "jobs": {},
        "data_cleanup_pending": True,
    }
    folder = FOLDER / "storage-probes" / operation
    cp = Host(load_hosts()["cp-01"])
    owned = {}

    def save():
        write_private(folder / "observation.json", report)
        write_private(FOLDER / "storage-probe.json", report)

    save()
    try:
        claim = json.loads(kubectl("-n", "klip", "get", "pvc", "klip-artifacts", "-o", "json"))
        if claim["status"].get("phase") != "Bound":
            raise ValueError("The retained artifact claim is not Bound")
        for stage in ("write", "read", "cleanup"):
            nodes = (
                ["platform-01"]
                if stage == "cleanup"
                else ["platform-01", "platform-02", "platform-03"]
            )
            for node in nodes:
                doc = job_document(operation, node, stage)
                name = doc["metadata"]["name"]
                write_private(folder / (name + ".job.json"), doc)
                current = json.loads(
                    cp.run(
                        [
                            "sudo",
                            "kubectl",
                            "--kubeconfig=/etc/kubernetes/admin.conf",
                            "create",
                            "-f",
                            "-",
                            "-o",
                            "json",
                        ],
                        data=json.dumps(doc),
                    )
                )
                owned[name] = current["metadata"]["uid"]
                report["jobs"][name] = {
                    "uid": owned[name],
                    "stage": stage,
                    "node": node,
                    "status": "running",
                }
                save()
            for node in nodes:
                name = f"storage-{stage}-{node}-{operation}"

                def terminal(name=name):
                    status = json.loads(kubectl("-n", "klip", "get", "job", name, "-o", "json"))[
                        "status"
                    ]
                    return any(
                        c["type"] in {"Complete", "Failed"} and c["status"] == "True"
                        for c in status.get("conditions", [])
                    )

                wait_until(terminal, "RWX storage canary", timeout=210, interval=3)
                current = json.loads(kubectl("-n", "klip", "get", "job", name, "-o", "json"))
                logs = kubectl("-n", "klip", "logs", "job/" + name)
                write_private(folder / (name + ".log"), logs)
                if (
                    current["metadata"]["uid"] != owned[name]
                    or not current["status"].get("succeeded")
                    or f"KLIP_STORAGE_{stage}_OK" not in logs
                ):
                    raise ValueError(
                        "Storage canary failed; partial files and private evidence retained"
                    )
                report["jobs"][name]["status"] = "passed"
                save()
        report.update(status="completed", completed_at=utc_now(), data_cleanup_pending=False)
    except Exception as error:
        report.update(status="failed", error_code=type(error).__name__)
        raise
    finally:
        report["job_cleanup_pending"] = True
        try:
            for name, uid in owned.items():
                current = json.loads(kubectl("-n", "klip", "get", "job", name, "-o", "json"))
                if current["metadata"]["uid"] != uid:
                    raise ValueError("Storage canary job ownership changed; cleanup refused")
                delete_owned_resource("/apis/batch/v1/namespaces/klip/jobs/" + name, uid)
                wait_until(
                    lambda name=name: (
                        not kubectl(
                            "-n", "klip", "get", "job", name, "--ignore-not-found", "-o", "json"
                        ).strip()
                    ),
                    "canary job cleanup",
                    timeout=60,
                )
            report["job_cleanup_pending"] = False
        except Exception:
            report["status"] = "failed"
            raise
        finally:
            save()
    return report
