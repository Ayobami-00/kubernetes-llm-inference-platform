"""Remove only a verified synthetic restore and its six UID-bound temporary disks."""

from __future__ import annotations

import argparse
import fcntl
import json
import re

from common import CONFIG, PROFILES, Aws, utc_now, write_private
from database import NAMESPACE
from database_recovery import RESTORE, assert_source, operation_paths, resource, sql
from smoke import wait_until


def assert_volume(volume: dict, pv: dict, claim: dict) -> None:
    tags = {t["Key"]: t["Value"] for t in volume.get("Tags", [])}
    if (
        not re.fullmatch(r"klip-db-restore-[123](-wal)?", claim["name"])
        or pv["metadata"]["uid"] != claim["pv_uid"]
        or pv["spec"]["csi"]["driver"] != "ebs.csi.aws.com"
        or pv["spec"]["csi"]["volumeHandle"] != claim["volume_id"]
        or pv["spec"]["claimRef"]
        != {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "name": claim["name"],
            "namespace": NAMESPACE,
            "resourceVersion": claim["claim_ref_version"],
            "uid": claim["uid"],
        }
        or volume["VolumeId"] != claim["volume_id"]
        or volume.get("Encrypted") is not True
        or volume.get("VolumeType") != "gp3"
        or volume.get("Size") != (20 if claim["name"].endswith("-wal") else 40)
        or volume.get("AvailabilityZone") != claim["az"]
        or any(
            tags.get(k) != v
            for k, v in {
                "ebs.csi.aws.com/cluster": "true",
                "klip.dev/cluster": CONFIG["cluster_id"],
                "Project": CONFIG["project"],
                "CSIVolumeName": pv["metadata"]["name"],
                "kubernetes.io/created-for/pvc/name": claim["name"],
                "kubernetes.io/created-for/pvc/namespace": NAMESPACE,
            }.items()
        )
    ):
        raise ValueError("Temporary restore disk identity, ownership or encryption differs")


def cleanup(operation: str) -> dict:
    from cluster import FOLDER, checked, delete_owned_resource, kubeconfigs, kubectl, tunnel
    from common import tool

    folder, backup_path = operation_paths(operation)
    backup = json.loads(backup_path.read_text())
    source = assert_source(backup)
    if source.get("status", {}).get("readyInstances") != 3:
        raise ValueError("Preserve the restored copy while the source database is unhealthy")
    verified = json.loads((folder / "verification.json").read_text())
    path = folder / "cleanup.json"
    aws = Aws(PROFILES["storage-access"])
    aws.preflight("storage-access")
    if path.exists():
        receipt = json.loads(path.read_text())
        if not receipt.get("cleanup_pending", True):
            return receipt
    else:
        current = resource("cluster", RESTORE)
        uid = current.get("metadata", {}).get("uid")
        if (
            not verified.get("base_marker_matches")
            or not verified.get("post_backup_wal_marker_matches")
            or verified["restore"]["cluster_uid"] != uid
        ):
            raise ValueError(
                "A matching successful restore verification is required before cleanup"
            )
        # This procedure deliberately cannot remove a copy containing application tables.
        count = sql(
            RESTORE,
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
            "ON c.relnamespace=n.oid WHERE c.relkind IN ('r','p') "
            "AND n.nspname NOT LIKE 'pg_%' "
            "AND n.nspname NOT IN ('information_schema','klip_verification');",
        )
        if count != "0":
            raise ValueError(
                "Cleanup is limited to the synthetic marker restore, not application data"
            )
        claims = json.loads(
            kubectl("-n", NAMESPACE, "get", "pvc", "-l", "cnpg.io/cluster=" + RESTORE, "-o", "json")
        )["items"]
        if len(claims) != 6:
            raise ValueError("Expected exactly six temporary restore claims")
        receipt = {
            "schema_version": "klip-database-restore-cleanup.v1",
            "operation": operation,
            "cluster_uid": uid,
            "started_at": utc_now(),
            "cleanup_pending": True,
            "claims": [],
        }
        for claim in claims:
            if not any(
                o.get("kind") == "Cluster" and o.get("uid") == uid
                for o in claim["metadata"].get("ownerReferences", [])
            ):
                raise ValueError("Temporary claim is not owned by the recorded restore cluster")
            pv = json.loads(kubectl("get", "pv", claim["spec"]["volumeName"], "-o", "json"))
            volume_id = pv["spec"]["csi"]["volumeHandle"]
            volume = aws.call("ec2", "describe-volumes", "--volume-ids", volume_id)["Volumes"][0]
            item = {
                "name": claim["metadata"]["name"],
                "uid": claim["metadata"]["uid"],
                "pv_name": pv["metadata"]["name"],
                "pv_uid": pv["metadata"]["uid"],
                "claim_ref_version": pv["spec"]["claimRef"]["resourceVersion"],
                "volume_id": volume_id,
                "az": volume["AvailabilityZone"],
            }
            assert_volume(volume, pv, item)
            receipt["claims"].append(item)
        write_private(path, receipt)  # Exact disk identities precede the first deletion.
    current = resource("cluster", RESTORE)
    if current:
        if current["metadata"]["uid"] != receipt["cluster_uid"]:
            raise ValueError("Restore name was reused; refusing cleanup")
        delete_owned_resource(
            "/apis/postgresql.cnpg.io/v1/namespaces/" + NAMESPACE + "/clusters/" + RESTORE,
            receipt["cluster_uid"],
        )
    wait_until(lambda: not resource("cluster", RESTORE), "restore Cluster removal", timeout=300)
    for item in receipt["claims"]:
        if item.get("deleted"):
            continue
        wait_until(
            lambda item=item: not resource("pvc", item["name"]),
            "owned restore claim removal",
            timeout=300,
        )

        # Reconcile a crash after CSI deletion but before the receipt was persisted.
        try:
            aws.call("ec2", "describe-volumes", "--volume-ids", item["volume_id"])
        except RuntimeError as error:
            if "InvalidVolume.NotFound" not in str(error):
                raise
            if kubectl("get", "pv", item["pv_name"], "--ignore-not-found", "-o", "json").strip():
                raise ValueError("A missing restore disk still has a Kubernetes PV") from error
            item["deleted"] = True
            write_private(path, receipt)
            continue

        def detached(item=item):
            volume = aws.call("ec2", "describe-volumes", "--volume-ids", item["volume_id"])[
                "Volumes"
            ][0]
            return (
                volume if not volume.get("Attachments") and volume["State"] == "available" else None
            )

        volume = wait_until(detached, "restore EBS detach", timeout=300)
        raw = kubectl("get", "pv", item["pv_name"], "--ignore-not-found", "-o", "json")
        if raw.strip():
            pv = json.loads(raw)
            assert_volume(volume, pv, item)
            if pv.get("status", {}).get("phase") != "Released":
                raise ValueError("Refusing to remove a bound restore volume")
            # UID + claim UID + disk ID + current policy are checked atomically by the API.
            patch = [
                {"op": "test", "path": "/metadata/uid", "value": item["pv_uid"]},
                {"op": "test", "path": "/spec/claimRef/uid", "value": item["uid"]},
                {"op": "test", "path": "/spec/csi/volumeHandle", "value": item["volume_id"]},
            ]
            policy = pv["spec"]["persistentVolumeReclaimPolicy"]
            if policy not in {"Retain", "Delete"}:
                raise ValueError("Unexpected temporary volume reclaim policy")
            if policy == "Retain":
                patch += [
                    {
                        "op": "test",
                        "path": "/spec/persistentVolumeReclaimPolicy",
                        "value": "Retain",
                    },
                    {
                        "op": "replace",
                        "path": "/spec/persistentVolumeReclaimPolicy",
                        "value": "Delete",
                    },
                ]
                kubectl("patch", "pv", item["pv_name"], "--type=json", "-p", json.dumps(patch))
            wait_until(
                lambda item=item: (
                    not kubectl(
                        "get", "pv", item["pv_name"], "--ignore-not-found", "-o", "json"
                    ).strip()
                ),
                "CSI temporary restore PV deletion",
                timeout=180,
            )

        def absent(item=item):
            try:
                aws.call("ec2", "describe-volumes", "--volume-ids", item["volume_id"])
            except RuntimeError as error:
                if "InvalidVolume.NotFound" in str(error):
                    return True
                raise
            return False

        wait_until(absent, "temporary restore disk deletion", timeout=120)
        item["deleted"] = True
        write_private(path, receipt)
    # The local chart keeps Cluster/ObjectStore metadata, but removes its schedule/policy.
    kubeconfigs()
    with tunnel():
        output = checked(
            [
                tool("helm"),
                "uninstall",
                RESTORE,
                "--ignore-not-found",
                "--namespace",
                NAMESPACE,
                "--kubeconfig",
                str(FOLDER / "operator.conf"),
                "--wait",
                "--timeout",
                "3m",
            ],
            timeout=240,
        )
    write_private(folder / "cleanup-helm.log", output)
    assert_source(backup)
    receipt.update(cleanup_pending=False, completed_at=utc_now(), source_database_preserved=True)
    write_private(path, receipt)
    return receipt


if __name__ == "__main__":
    from cluster import FOLDER

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", required=True)
    args = parser.parse_args()
    with (FOLDER / "operator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = cleanup(args.operation)
    print(json.dumps(result, indent=2))
