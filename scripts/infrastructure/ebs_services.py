"""Scoped EBS controller identity and pinned, IMDS-independent driver configuration."""

from __future__ import annotations

import json

from common import CONFIG, PROFILES, Aws, source_digest, state_home, tf, utc_now, write_private
from platform_storage import assert_policy

SIDECARS = {
    "provisioner": ("public.ecr.aws/csi-components/csi-provisioner", "v6.4.0-eksbuild.3"),
    "attacher": ("public.ecr.aws/csi-components/csi-attacher", "v4.13.0-eksbuild.3"),
    "snapshotter": ("public.ecr.aws/csi-components/csi-snapshotter", "v8.7.0-eksbuild.2"),
    "livenessProbe": ("public.ecr.aws/csi-components/livenessprobe", "v2.20.0-eksbuild.3"),
    "resizer": ("public.ecr.aws/csi-components/csi-resizer", "v2.3.0-eksbuild.3"),
    "nodeDriverRegistrar": (
        "public.ecr.aws/csi-components/csi-node-driver-registrar",
        "v2.18.0-eksbuild.3",
    ),
    "volumemodifier": (
        "public.ecr.aws/ebs-csi-driver/volume-modifier-for-k8s",
        "v0.9.6-eksbuild.4",
    ),
}


def values_for_ebs(allocation: dict | None, digest) -> dict:
    role = f"arn:aws:iam::{CONFIG['account_id']}:role/{CONFIG['project']}/klip-ebs-controller"
    if allocation is None or allocation.get("ebs_controller_role_arn") != role:
        raise ValueError("EBS requires the applied exact controller role")
    placement = {"klip.dev.node-restriction.kubernetes.io/platform": "true"}
    labels = {"app": "ebs-csi-controller", "app.kubernetes.io/instance": "aws-ebs-csi-driver"}
    environment = [{"name": "AWS_EC2_METADATA_DISABLED", "value": "true"}]
    return {
        # Remove the upstream optional static-key Secret fallback entirely.
        "awsAccessSecret": None,
        "image": {
            "tag": "v1.67.2@" + digest("public.ecr.aws/ebs-csi-driver/aws-ebs-csi-driver:v1.67.2")
        },
        "sidecars": {
            name: {
                "image": {"tag": tag + "@" + digest(repository + ":" + tag)},
                "resources": {
                    "requests": {"cpu": "30m", "memory": "32Mi"},
                    "limits": {"cpu": "500m", "memory": "128Mi"},
                },
            }
            for name, (repository, tag) in SIDECARS.items()
        },
        "controller": {
            "region": CONFIG["region"],
            "replicaCount": 2,
            "nodeSelector": placement,
            "env": environment,
            "tolerations": [],
            "sdkDebugLog": False,
            "serviceAccount": {
                "annotations": {
                    "eks.amazonaws.com/role-arn": role,
                    "eks.amazonaws.com/token-expiration": "3600",
                    "eks.amazonaws.com/sts-regional-endpoints": "true",
                }
            },
            "extraVolumeTags": {
                "Project": CONFIG["project"],
                "Environment": CONFIG["environment"],
                "klip.dev/cluster": CONFIG["cluster_id"],
            },
            "resources": {
                "requests": {"cpu": "100m", "memory": "128Mi"},
                "limits": {"cpu": "1", "memory": "512Mi"},
            },
            "podDisruptionBudget": {"enabled": True},
            "updateStrategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
            },
            "affinity": {
                "nodeAffinity": None,
                "podAntiAffinity": {
                    "preferredDuringSchedulingIgnoredDuringExecution": None,
                    "requiredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "topologyKey": "kubernetes.io/hostname",
                            "labelSelector": {"matchLabels": labels},
                        }
                    ],
                },
            },
            "topologySpreadConstraints": [
                {
                    "topologyKey": "topology.kubernetes.io/zone",
                    "whenUnsatisfiable": "DoNotSchedule",
                    "maxSkew": 1,
                    "labelSelector": {"matchLabels": labels},
                }
            ],
        },
        "node": {
            "enableWindows": False,
            "metadataSources": "kubernetes",
            "volumeAttachLimit": 10,
            "nodeSelector": placement,
            "env": environment,
            "tolerations": [],
            "serviceAccount": {"disableMutation": True},
            "updateStrategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1}},
            "resources": {
                "requests": {"cpu": "100m", "memory": "128Mi"},
                "limits": {"cpu": "1", "memory": "512Mi"},
            },
        },
        "storageClasses": [
            {
                "name": "klip-gp3",
                "reclaimPolicy": "Retain",
                "allowVolumeExpansion": True,
                "volumeBindingMode": "WaitForFirstConsumer",
                "parameters": {
                    "type": "gp3",
                    "encrypted": "true",
                    "csi.storage.k8s.io/fstype": "ext4",
                    "iops": "3000",
                    "throughput": "125",
                },
                "allowedTopologies": [
                    {
                        "matchLabelExpressions": [
                            {
                                "key": "topology.ebs.csi.aws.com/zone",
                                "values": sorted(
                                    {n["availability_zone"] for n in CONFIG["nodes"].values()}
                                ),
                            }
                        ]
                    }
                ],
            }
        ],
    }


def verify_ebs_identity() -> dict:
    aws = Aws(PROFILES["storage-access"])
    aws.preflight("storage-access")
    allocation = json.loads(tf("storage-access", "output", "-json", "storage_access_manifest"))
    for key in ("account_id", "region", "project", "cluster_id"):
        if allocation[key] != CONFIG[key]:
            raise ValueError("Unexpected EBS access-layer identity")
    state = json.loads(tf("storage-access", "show", "-json"))
    resources = {r["address"]: r["values"] for r in state["values"]["root_module"]["resources"]}
    expected = resources["aws_iam_role.ebs"]
    actual = aws.call("iam", "get-role", "--role-name", expected["name"])["Role"]
    if (
        actual["Arn"] != allocation["ebs_controller_role_arn"]
        or actual.get("MaxSessionDuration") != expected["max_session_duration"]
    ):
        raise ValueError("Unexpected live EBS controller role")
    assert_policy(actual["AssumeRolePolicyDocument"], expected["assume_role_policy"], "EBS trust")
    if aws.call("iam", "list-attached-role-policies", "--role-name", expected["name"])[
        "AttachedPolicies"
    ]:
        raise ValueError("EBS role has unexpected attached permissions")
    policy = resources["aws_iam_role_policy.ebs"]
    if aws.call("iam", "list-role-policies", "--role-name", expected["name"])["PolicyNames"] != [
        policy["name"]
    ]:
        raise ValueError("EBS role has unexpected additional inline policies")
    live = aws.call(
        "iam", "get-role-policy", "--role-name", expected["name"], "--policy-name", policy["name"]
    )["PolicyDocument"]
    assert_policy(live, policy["policy"], "EBS permissions")
    write_private(
        state_home() / "reports/ebs-identity.json",
        {
            "captured_at": utc_now(),
            "source_sha256": source_digest(),
            "role_arn": actual["Arn"],
            "policies_match_applied_state": True,
        },
    )
    return allocation
