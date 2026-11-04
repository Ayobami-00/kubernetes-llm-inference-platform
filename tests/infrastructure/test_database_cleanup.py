"""Temporary restore cleanup cannot be redirected to production or foreign disks."""

import copy

import pytest
from common import CONFIG
from database_cleanup import assert_volume


def fixture():
    claim = {
        "name": "klip-db-restore-1",
        "uid": "claim-uid",
        "pv_uid": "pv-uid",
        "claim_ref_version": "123",
        "volume_id": "vol-00000000000000001",
        "az": "eu-west-2a",
    }
    pv = {
        "metadata": {"name": "pvc-claim-uid", "uid": "pv-uid"},
        "spec": {
            "csi": {"driver": "ebs.csi.aws.com", "volumeHandle": claim["volume_id"]},
            "claimRef": {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "name": claim["name"],
                "namespace": "platform-data",
                "resourceVersion": "123",
                "uid": "claim-uid",
            },
        },
    }
    volume = {
        "VolumeId": claim["volume_id"],
        "Encrypted": True,
        "VolumeType": "gp3",
        "Size": 40,
        "AvailabilityZone": "eu-west-2a",
        "Tags": [
            {"Key": k, "Value": v}
            for k, v in {
                "ebs.csi.aws.com/cluster": "true",
                "klip.dev/cluster": CONFIG["cluster_id"],
                "Project": CONFIG["project"],
                "CSIVolumeName": "pvc-claim-uid",
                "kubernetes.io/created-for/pvc/name": claim["name"],
                "kubernetes.io/created-for/pvc/namespace": "platform-data",
            }.items()
        ],
    }
    return volume, pv, claim


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("claim", "name", "klip-db-1"),
        ("claim", "uid", "reused-name"),
        ("claim", "pv_uid", "foreign-pv"),
        ("volume", "Encrypted", False),
        ("volume", "Size", 200),
        ("volume", "Tags", []),
        ("volume", "AvailabilityZone", "eu-west-2b"),
    ],
)
def test_foreign_or_changed_restore_disk_cannot_be_deleted(section, field, value):
    volume, pv, claim = fixture()
    assert_volume(volume, pv, claim)
    changed = copy.deepcopy(claim if section == "claim" else volume)
    changed[field] = value
    with pytest.raises(ValueError, match="disk identity"):
        assert_volume(
            changed if section == "volume" else volume, pv, changed if section == "claim" else claim
        )
