"""Observe retained storage and compare live security policies with applied Terraform state."""

from __future__ import annotations

import argparse
import json

from common import CONFIG, PROFILES, Aws, source_digest, state_home, tf, utc_now, write_private


def canonical_policy(value):
    """IAM statement/action/resource order has no meaning; preserve all actual values."""
    if isinstance(value, str):
        return canonical_policy(json.loads(value))
    if isinstance(value, dict):
        return {
            key: canonical_policy(nested) if isinstance(nested, (dict, list)) else nested
            for key, nested in sorted(value.items())
        }
    if isinstance(value, list):
        if len(value) == 1:
            item = value[0]
            return canonical_policy(item) if isinstance(item, (dict, list)) else item
        return sorted(
            (canonical_policy(item) if isinstance(item, (dict, list)) else item for item in value),
            key=lambda item: json.dumps(item, sort_keys=True),
        )
    return value


def assert_policy(actual, expected, label):
    if canonical_policy(actual) != canonical_policy(expected):
        raise ValueError(f"Live {label} differs from applied policy; storage verification failed")


def verify_connectivity(aws, allocation: dict) -> dict:
    from cluster import Host, load_hosts

    access = json.loads(tf("storage-access", "output", "-json", "storage_access_manifest"))
    workers = json.loads(tf("platform", "output", "-json", "platform_manifest"))
    for key in ("account_id", "region", "project", "cluster_id"):
        if access[key] != CONFIG[key] or workers[key] != CONFIG[key]:
            raise ValueError("Unexpected storage-connectivity identity")
    if access["vpc_id"] != workers["network"]["vpc_id"] or set(access["mount_targets"]) != set(
        workers["nodes"]
    ):
        raise ValueError("Mount targets and platform allocation differ")
    fs = allocation["artifacts"]["file_system_id"]
    actual = aws.call("efs", "describe-mount-targets", "--file-system-id", fs)["MountTargets"]
    by_id = {item["MountTargetId"]: item for item in actual}
    if set(by_id) != {item["id"] for item in access["mount_targets"].values()}:
        raise ValueError("Unexpected missing or additional EFS mount targets")
    groups = aws.call(
        "ec2", "describe-security-groups", "--group-ids", access["security_group_id"]
    )["SecurityGroups"]
    if (
        len(groups) != 1
        or groups[0]["VpcId"] != access["vpc_id"]
        or groups[0]["IpPermissions"]
        != [
            {
                "IpProtocol": "tcp",
                "FromPort": 2049,
                "ToPort": 2049,
                "UserIdGroupPairs": [
                    {
                        "UserId": CONFIG["account_id"],
                        "GroupId": workers["network"]["security_group_id"],
                    }
                ],
                "IpRanges": [],
                "Ipv6Ranges": [],
                "PrefixListIds": [],
            }
        ]
        or groups[0]["IpPermissionsEgress"]
    ):
        raise ValueError("EFS security group is not restricted to CPU NFS clients")
    result = {}
    hosts = load_hosts()
    for name, target in access["mount_targets"].items():
        live = by_id[target["id"]]
        if (
            live["LifeCycleState"] != "available"
            or live["VpcId"] != access["vpc_id"]
            or live["IpAddress"] != target["ip_address"]
            or live["AvailabilityZoneName"] != workers["nodes"][name]["availability_zone"]
        ):
            raise ValueError("Mount target is not available in its reviewed worker AZ")
        security = aws.call(
            "efs", "describe-mount-target-security-groups", "--mount-target-id", target["id"]
        )["SecurityGroups"]
        if security != [access["security_group_id"]]:
            raise ValueError("Unexpected EFS mount-target access group")
        Host(hosts[name]).run(
            [
                "python3",
                "-c",
                "import socket,sys; socket.create_connection((sys.argv[1],2049),5).close()",
                target["ip_address"],
            ],
            timeout=30,
        )
        result[name] = {"mount_target_id": target["id"], "local_az_tcp_reachable": True}
    return result


def verify_storage() -> dict:
    aws = Aws(PROFILES["storage"])
    aws.preflight("storage")
    allocation = json.loads(tf("storage", "output", "-json", "storage_manifest"))
    for key in ("account_id", "region", "project", "cluster_id"):
        if allocation[key] != CONFIG[key]:
            raise ValueError("Unexpected retained storage identity")
    resources = {
        item["address"]: item["values"]
        for item in json.loads(tf("storage", "show", "-json"))["values"]["root_module"]["resources"]
    }
    store = allocation["artifacts"]
    fs = aws.call("efs", "describe-file-systems", "--file-system-id", store["file_system_id"])[
        "FileSystems"
    ][0]
    if (
        fs["LifeCycleState"] != "available"
        or not fs["Encrypted"]
        or fs.get("AvailabilityZoneName")
        or fs["ThroughputMode"] != "elastic"
    ):
        raise ValueError("Artifact filesystem is not available/encrypted/regional/elastic")
    ap = aws.call("efs", "describe-access-points", "--access-point-id", store["access_point_id"])[
        "AccessPoints"
    ][0]
    if (
        ap["FileSystemId"] != fs["FileSystemId"]
        or ap["LifeCycleState"] != "available"
        or ap["PosixUser"] != {"Uid": 10001, "Gid": 10001}
        or ap["RootDirectory"]
        != {
            "Path": "/artifacts",
            "CreationInfo": {"OwnerUid": 10001, "OwnerGid": 10001, "Permissions": "0770"},
        }
    ):
        raise ValueError("Artifact access point identity/permissions differ")
    assert_policy(
        aws.call("efs", "describe-file-system-policy", "--file-system-id", fs["FileSystemId"])[
            "Policy"
        ],
        resources["aws_efs_file_system_policy.artifacts"]["policy"],
        "EFS access",
    )
    if (
        aws.call("efs", "describe-backup-policy", "--file-system-id", fs["FileSystemId"])[
            "BackupPolicy"
        ]["Status"]
        != "ENABLED"
    ):
        raise ValueError("Automatic artifact backups are not enabled")
    bucket = allocation["database"]["bucket"]
    args = ["--bucket", bucket, "--expected-bucket-owner", CONFIG["account_id"]]
    if aws.call("s3api", "get-bucket-versioning", *args).get("Status") != "Enabled":
        raise ValueError("Database archive versioning is disabled")
    if not all(
        aws.call("s3api", "get-public-access-block", *args)[
            "PublicAccessBlockConfiguration"
        ].values()
    ):
        raise ValueError("Database archive is not fully public-access blocked")
    encryption = aws.call("s3api", "get-bucket-encryption", *args)[
        "ServerSideEncryptionConfiguration"
    ]["Rules"]
    if (
        len(encryption) != 1
        or encryption[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] != "AES256"
    ):
        raise ValueError("Database archive encryption differs")
    ownership = aws.call("s3api", "get-bucket-ownership-controls", *args)["OwnershipControls"][
        "Rules"
    ]
    if ownership != [{"ObjectOwnership": "BucketOwnerEnforced"}]:
        raise ValueError("Database archive ownership is not enforced")
    lifecycle = aws.call("s3api", "get-bucket-lifecycle-configuration", *args)["Rules"]
    if (
        len(lifecycle) != 1
        or lifecycle[0].get("Expiration")
        or lifecycle[0].get("Filter") != {"Prefix": "postgresql/"}
        or lifecycle[0].get("Status") != "Enabled"
        or lifecycle[0].get("NoncurrentVersionExpiration") != {"NoncurrentDays": 45}
        or lifecycle[0].get("AbortIncompleteMultipartUpload") != {"DaysAfterInitiation": 7}
    ):
        raise ValueError("Database archive lifecycle could invalidate recovery retention")
    assert_policy(
        aws.call("s3api", "get-bucket-policy", *args)["Policy"],
        resources["aws_s3_bucket_policy.database"]["policy"],
        "archive TLS",
    )
    for name in ("artifacts", "database"):
        role = resources[f'aws_iam_role.storage["{name}"]']
        live = aws.call("iam", "get-role", "--role-name", role["name"])["Role"]
        if live["Arn"] != role["arn"]:
            raise ValueError("Unexpected storage role ARN")
        assert_policy(
            live["AssumeRolePolicyDocument"], role["assume_role_policy"], name + " role trust"
        )
        if aws.call("iam", "list-attached-role-policies", "--role-name", role["name"])[
            "AttachedPolicies"
        ]:
            raise ValueError("Storage role has unexpected attached permissions")
        policy = resources[f"aws_iam_role_policy.{name}"]
        names = aws.call("iam", "list-role-policies", "--role-name", role["name"])["PolicyNames"]
        if names != [policy["name"]]:
            raise ValueError("Unexpected additional storage role policy")
        actual = aws.call(
            "iam", "get-role-policy", "--role-name", role["name"], "--policy-name", policy["name"]
        )["PolicyDocument"]
        assert_policy(actual, policy["policy"], name + " permissions")
    connectivity = verify_connectivity(aws, allocation)
    result = {
        "schema_version": "klip-platform-storage-verification.v1",
        "captured_at": utc_now(),
        "source_sha256": source_digest(),
        "allocation": allocation,
        "connectivity": connectivity,
        "checks": {
            "regional_encryption": True,
            "access_point_permissions": True,
            "efs_tls_path_root_denials": True,
            "archive_tls_versioning_retention": True,
            "exact_workload_roles": True,
            "automatic_efs_backup_enabled": True,
        },
        "not_yet_verified": [
            "CSI mounts",
            "database WAL archiving",
            "successful backup recovery points",
            "actual restore",
            "long-running credential renewal",
        ],
    }
    write_private(state_home() / "reports/platform-storage.json", result)
    return result


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(json.dumps(verify_storage(), indent=2))
