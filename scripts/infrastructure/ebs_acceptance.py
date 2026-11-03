"""Three bounded gp3 canaries: provision, detach/remount, retain, then owned cleanup."""

from __future__ import annotations

import json
import uuid

from common import CONFIG, PROFILES, Aws, source_digest, tf, utc_now, write_private
from smoke import wait_until
from storage_acceptance import job_document as filesystem_job


def volume_identity(volume: dict, pv: dict, namespace: str, claim: str, node: dict) -> None:
    tags = {t["Key"]: t["Value"] for t in volume.get("Tags", [])}
    expected = {
        "ebs.csi.aws.com/cluster": "true",
        "klip.dev/cluster": CONFIG["cluster_id"],
        "Project": CONFIG["project"],
        "CSIVolumeName": pv["metadata"]["name"],
        "kubernetes.io/created-for/pvc/name": claim,
        "kubernetes.io/created-for/pvc/namespace": namespace,
    }
    if (
        volume["VolumeId"] != pv["spec"]["csi"]["volumeHandle"]
        or volume.get("Encrypted") is not True
        or volume.get("VolumeType") != "gp3"
        or volume.get("Size") != 1
        or volume.get("AvailabilityZone") != node["availability_zone"]
        or any(tags.get(k) != v for k, v in expected.items())
    ):
        raise ValueError("Canary volume ownership, encryption, size or topology differs")
    if any(item["InstanceId"] != node["instance_id"] for item in volume.get("Attachments", [])):
        raise ValueError("Canary volume is attached to an unexpected instance")


def job_document(operation: str, node: str, stage: str, namespace: str) -> dict:
    doc = filesystem_job(operation, node, stage)
    if stage not in {"write", "read"} or namespace != "klip-ebs-check-" + operation:
        raise ValueError("Unexpected EBS canary stage/namespace")
    doc["metadata"].update(name=f"ebs-{stage}-{node}-{operation}", namespace=namespace)
    spec = doc["spec"]["template"]["spec"]
    spec["volumes"][0]["persistentVolumeClaim"]["claimName"] = node
    spec["containers"][0]["command"][2] = """
set -eu
base=/artifacts/.verification-$KLIP_OPERATION
if test "$KLIP_STAGE" = write; then
  umask 077
  mkdir -p "$base"
  printf '%s\n' "$KLIP_OPERATION:$KLIP_NODE" > "$base/value"
  sync -f "$base/value"
  test "$(stat -c %u "$base/value")" = 10001
else
  test "$(cat "$base/value")" = "$KLIP_OPERATION:$KLIP_NODE"
fi
printf 'KLIP_EBS_%s_OK\n' "$KLIP_STAGE"
"""
    return doc


def verify() -> dict:
    from cluster import FOLDER, Host, delete_owned_resource, kubectl, load_hosts

    aws = Aws(PROFILES["storage-access"])
    aws.preflight("storage-access")
    path = FOLDER / "ebs-probe.json"
    if path.exists():
        previous = json.loads(path.read_text())
        if previous.get("cleanup_pending", True):
            raise ValueError(
                "A prior EBS canary needs inspection/owned cleanup before another allocation"
            )
    allocation = json.loads(tf("platform", "output", "-json", "platform_manifest"))
    for key in ("account_id", "region", "cluster_id", "project"):
        if allocation[key] != CONFIG[key]:
            raise ValueError("Unexpected EBS canary platform allocation")
    operation = uuid.uuid4().hex[:12]
    namespace = "klip-ebs-check-" + operation
    report = {
        "schema_version": "klip-ebs-canary.v1",
        "operation_id": operation,
        "status": "running",
        "source_sha256": source_digest(),
        "started_at": utc_now(),
        "namespace": namespace,
        "namespace_uid": None,
        "claims": {},
        "jobs": {},
        "volumes": {},
        "cleanup_pending": True,
    }
    folder = FOLDER / "ebs-probes" / operation
    cp = Host(load_hosts()["cp-01"])

    def save():
        write_private(path, report)
        write_private(folder / "observation.json", report)

    def create(doc):
        return json.loads(
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

    def job_done(name):
        job = json.loads(kubectl("-n", namespace, "get", "job", name, "-o", "json"))
        return any(
            c["type"] in {"Complete", "Failed"} and c["status"] == "True"
            for c in job.get("status", {}).get("conditions", [])
        )

    def volume_exists(identity):
        try:
            aws.call("ec2", "describe-volumes", "--volume-ids", identity)
            return True
        except RuntimeError as error:
            if "(InvalidVolume.NotFound)" in str(error):
                return False
            raise

    save()
    try:
        storage = json.loads(kubectl("get", "storageclass", "klip-gp3", "-o", "json"))
        if (
            storage["volumeBindingMode"] != "WaitForFirstConsumer"
            or storage["reclaimPolicy"] != "Retain"
            or storage["parameters"].get("encrypted") != "true"
        ):
            raise ValueError("EBS canary requires the protected retained topology-aware class")
        ns = create(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": namespace,
                    "annotations": {"klip.dev/verification-id": operation},
                    "labels": {"pod-security.kubernetes.io/enforce": "restricted"},
                },
            }
        )
        report["namespace_uid"] = ns["metadata"]["uid"]
        save()
        for node in sorted(allocation["nodes"]):
            claim = create(
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "metadata": {"name": node, "namespace": namespace},
                    "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        "storageClassName": "klip-gp3",
                        "resources": {"requests": {"storage": "1Gi"}},
                    },
                }
            )
            report["claims"][node] = {"uid": claim["metadata"]["uid"]}
            if claim["spec"].get("volumeName"):
                raise ValueError("A WFFC claim bound before any consumer existed")
            save()
        for stage in ("write", "read"):
            for node in sorted(allocation["nodes"]):
                doc = job_document(operation, node, stage, namespace)
                write_private(folder / (doc["metadata"]["name"] + ".job.json"), doc)
                job = create(doc)
                name = job["metadata"]["name"]
                report["jobs"][name] = {"uid": job["metadata"]["uid"], "status": "running"}
                save()
            for node in sorted(allocation["nodes"]):
                name = f"ebs-{stage}-{node}-{operation}"
                wait_until(
                    lambda name=name: job_done(name), "EBS mounted canary", timeout=210, interval=3
                )
                job = json.loads(kubectl("-n", namespace, "get", "job", name, "-o", "json"))
                logs = kubectl("-n", namespace, "logs", "job/" + name)
                write_private(folder / (name + ".log"), logs)
                if (
                    job["metadata"]["uid"] != report["jobs"][name]["uid"]
                    or not job["status"].get("succeeded")
                    or f"KLIP_EBS_{stage}_OK" not in logs
                ):
                    raise ValueError(
                        "EBS filesystem canary failed; bounded resources retained for diagnosis"
                    )
                claim = json.loads(kubectl("-n", namespace, "get", "pvc", node, "-o", "json"))
                if claim["metadata"]["uid"] != report["claims"][node]["uid"]:
                    raise ValueError("EBS canary claim identity changed")
                pv = json.loads(kubectl("get", "pv", claim["spec"]["volumeName"], "-o", "json"))
                if (
                    pv["spec"]["claimRef"]["uid"] != claim["metadata"]["uid"]
                    or pv["spec"]["persistentVolumeReclaimPolicy"] != "Retain"
                ):
                    raise ValueError("EBS PV binding/retention differs")
                identity = pv["spec"]["csi"]["volumeHandle"]
                volume = aws.call("ec2", "describe-volumes", "--volume-ids", identity)["Volumes"][0]
                volume_identity(volume, pv, namespace, node, allocation["nodes"][node])
                report["volumes"][node] = {
                    "volume_id": identity,
                    "pv_name": pv["metadata"]["name"],
                    "pv_uid": pv["metadata"]["uid"],
                    "az": volume["AvailabilityZone"],
                    "encrypted": True,
                }
                report["jobs"][name]["status"] = "passed"
                delete_owned_resource(
                    "/apis/batch/v1/namespaces/" + namespace + "/jobs/" + name,
                    job["metadata"]["uid"],
                )
                save()
            # New readers must attach/mount after all previous writer attachments disappear.
            expected_pvs = {v["pv_name"] for v in report["volumes"].values()}
            wait_until(
                lambda expected_pvs=expected_pvs: (
                    not any(
                        v["spec"]["source"].get("persistentVolumeName") in expected_pvs
                        for v in json.loads(kubectl("get", "volumeattachments", "-o", "json"))[
                            "items"
                        ]
                    )
                ),
                "EBS detach between consumer cohorts",
                timeout=180,
                interval=3,
            )
        delete_owned_resource("/api/v1/namespaces/" + namespace, report["namespace_uid"])
        wait_until(
            lambda: (
                not kubectl(
                    "get", "namespace", namespace, "--ignore-not-found", "-o", "json"
                ).strip()
            ),
            "EBS namespace cleanup",
            timeout=90,
            interval=3,
        )
        report["retention_verified"] = {}
        for node, item in report["volumes"].items():
            wait_until(
                lambda item=item: (
                    json.loads(kubectl("get", "pv", item["pv_name"], "-o", "json"))["status"].get(
                        "phase"
                    )
                    == "Released"
                ),
                "Retain after claim deletion",
                timeout=90,
                interval=3,
            )
            pv = json.loads(kubectl("get", "pv", item["pv_name"], "-o", "json"))
            volume = aws.call("ec2", "describe-volumes", "--volume-ids", item["volume_id"])[
                "Volumes"
            ][0]
            volume_identity(volume, pv, namespace, node, allocation["nodes"][node])
            if (
                volume.get("Attachments")
                or pv["metadata"]["uid"] != item["pv_uid"]
                or pv["spec"]["claimRef"]["uid"] != report["claims"][node]["uid"]
            ):
                raise ValueError("Cannot clean up an attached or changed canary identity")
            report["retention_verified"][node] = True
            save()
            # Only these verified 1-GiB synthetic canaries switch to Delete. Real data stays Retain.
            patch = [
                {"op": "test", "path": "/metadata/uid", "value": item["pv_uid"]},
                {"op": "test", "path": "/spec/csi/volumeHandle", "value": item["volume_id"]},
                {
                    "op": "test",
                    "path": "/spec/claimRef/uid",
                    "value": report["claims"][node]["uid"],
                },
                {"op": "test", "path": "/spec/persistentVolumeReclaimPolicy", "value": "Retain"},
                {"op": "replace", "path": "/spec/persistentVolumeReclaimPolicy", "value": "Delete"},
            ]
            kubectl("patch", "pv", item["pv_name"], "--type=json", "-p", json.dumps(patch))
            wait_until(
                lambda item=item: (
                    not kubectl(
                        "get", "pv", item["pv_name"], "--ignore-not-found", "-o", "json"
                    ).strip()
                ),
                "CSI synthetic canary PV cleanup",
                timeout=180,
                interval=3,
            )
            wait_until(
                lambda item=item: not volume_exists(item["volume_id"]),
                "Synthetic EBS deletion confirmation",
                timeout=90,
                interval=3,
            )
            item["deleted"] = True
            save()
        report.update(status="completed", completed_at=utc_now(), cleanup_pending=False)
    except Exception as error:
        report.update(status="failed", error_code=type(error).__name__)
        raise
    finally:
        save()
    return report
