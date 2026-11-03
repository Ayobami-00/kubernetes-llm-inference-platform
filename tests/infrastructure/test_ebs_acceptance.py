"""Canary cleanup cannot accept production-sized, foreign or attached-to-foreign disks."""

import copy

import pytest
from common import CONFIG
from ebs_acceptance import job_document, volume_identity


def fixture():
    pv = {"metadata": {"name": "pvc-fixture"}, "spec": {"csi": {"volumeHandle": "vol-fixture"}}}
    node = {"instance_id": "i-platform", "availability_zone": "eu-west-2a"}
    volume = {
        "VolumeId": "vol-fixture",
        "Encrypted": True,
        "VolumeType": "gp3",
        "Size": 1,
        "AvailabilityZone": "eu-west-2a",
        "Attachments": [{"InstanceId": "i-platform"}],
        "Tags": [
            {"Key": k, "Value": v}
            for k, v in {
                "ebs.csi.aws.com/cluster": "true",
                "klip.dev/cluster": CONFIG["cluster_id"],
                "Project": CONFIG["project"],
                "CSIVolumeName": "pvc-fixture",
                "kubernetes.io/created-for/pvc/name": "platform-01",
                "kubernetes.io/created-for/pvc/namespace": "fixture-namespace",
            }.items()
        ],
    }
    return volume, pv, node


@pytest.mark.parametrize(
    "field,value",
    [
        ("VolumeId", "foreign"),
        ("Encrypted", False),
        ("Size", 40),
        ("VolumeType", "io2"),
        ("AvailabilityZone", "eu-west-2b"),
        ("Tags", []),
        ("Attachments", [{"InstanceId": "i-control-plane"}]),
    ],
)
def test_disk_drift_and_production_volumes_are_rejected(field, value):
    volume, pv, node = fixture()
    volume_identity(volume, pv, "fixture-namespace", "platform-01", node)
    changed = copy.deepcopy(volume)
    changed[field] = value
    with pytest.raises(ValueError):
        volume_identity(changed, pv, "fixture-namespace", "platform-01", node)


def test_bounded_filesystem_jobs_have_no_cloud_or_api_credentials():
    doc = job_document("0123456789ab", "platform-01", "write", "klip-ebs-check-0123456789ab")
    pod = doc["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsUser"] == 10001
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == "platform-01"
    assert all(not e["name"].startswith("AWS_ROLE") for e in pod["containers"][0]["env"])
    assert doc["spec"]["backoffLimit"] == 0
    with pytest.raises(ValueError):
        job_document("0123456789ab", "platform-01", "cleanup", "foreign")
