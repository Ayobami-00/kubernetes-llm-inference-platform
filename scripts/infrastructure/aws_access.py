"""Render the reviewed, one-time AWS access bootstrap documents; perform no AWS calls."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

PROJECT = "kubernetes-llm-inference-platform"
ROLE_NAME = "kubernetes-llm-foundation-deployer"
DNS_ROLE_NAME = "kubernetes-llm-inference-dns-deployer"
IAM_PATH = f"/{PROJECT}/"


def allow(sid: str, actions: list[str], resources: list[str], **conditions: dict) -> dict:
    statement = {"Sid": sid, "Effect": "Allow", "Action": actions, "Resource": resources}
    if conditions:
        statement["Condition"] = conditions
    return statement


def policy(*statements: dict) -> dict:
    return {"Version": "2012-10-17", "Statement": list(statements)}


def build_documents(
    account_id: str,
    region: str,
    source_user: str = PROJECT,
    hosted_zone_id: str = "Z06158312NEW6IHHPH5XV",
    inference_hostname: str = "inference.klip-lab.click",
) -> dict[str, dict]:
    if not re.fullmatch(r"[0-9]{12}", account_id):
        raise ValueError("account ID must contain exactly twelve digits")
    if region != "eu-west-2":
        raise ValueError("this reviewed access policy supports eu-west-2 only")
    if not re.fullmatch(r"[A-Za-z0-9+=,.@_-]{1,64}", source_user):
        raise ValueError("source user must be an IAM user name without a path")
    if not re.fullmatch(r"Z[A-Z0-9]{1,31}", hosted_zone_id):
        raise ValueError("hosted zone ID must be a Route 53 zone identifier")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", inference_hostname):
        raise ValueError("inference hostname must be a normalized DNS name")

    ec2 = f"arn:aws:ec2:{region}:{account_id}"
    iam = f"arn:aws:iam::{account_id}"
    elb = f"arn:aws:elasticloadbalancing:{region}:{account_id}"
    node_role = f"{iam}:role{IAM_PATH}klip-control-plane"
    node_profile = f"{iam}:instance-profile{IAM_PATH}klip-control-plane"
    boundary = f"{iam}:policy{IAM_PATH}kubernetes-llm-control-plane-boundary"
    ssm_core = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
    state_bucket = f"klip-tfstate-{account_id}-{region}"
    state = f"arn:aws:s3:::{state_bucket}/aws-foundation/terraform.tfstate"
    regional = {"aws:RequestedRegion": region}
    owned = {**regional, "ec2:ResourceTag/Project": PROJECT}
    create = {**regional, "aws:RequestTag/Project": PROJECT}

    network_create = [
        "ec2:AllocateAddress",
        "ec2:CreateInternetGateway",
        "ec2:CreateRouteTable",
        "ec2:CreateSecurityGroup",
        "ec2:CreateSubnet",
        "ec2:CreateVpc",
    ]
    network = policy(
        allow(
            "ReadRegionalFoundationMetadata",
            [
                "ec2:DescribeAddresses",
                "ec2:DescribeAvailabilityZones",
                "ec2:DescribeImages",
                "ec2:DescribeInstanceCreditSpecifications",
                "ec2:DescribeInstanceStatus",
                "ec2:DescribeInstanceTypes",
                "ec2:DescribeInstances",
                "ec2:DescribeInternetGateways",
                "ec2:DescribeKeyPairs",
                "ec2:DescribeNetworkInterfaces",
                "ec2:DescribeRouteTables",
                "ec2:DescribeSecurityGroupRules",
                "ec2:DescribeSecurityGroups",
                "ec2:DescribeSubnets",
                "ec2:DescribeTags",
                "ec2:DescribeVolumes",
                "ec2:DescribeVolumesModifications",
                "ec2:DescribeVpcAttribute",
                "ec2:DescribeVpcs",
                "ec2:DescribeIamInstanceProfileAssociations",
                "ec2:DescribeInstanceTypeOfferings",
                "ssm:DescribeInstanceInformation",
            ],
            ["*"],
            StringEquals=regional,
        ),
        allow("CreateTaggedNetwork", network_create, ["*"], StringEquals=create),
        allow(
            "ManageOwnedNetwork",
            [
                "ec2:AssociateAddress",
                "ec2:DisassociateAddress",
                "ec2:ReleaseAddress",
                "ec2:AttachInternetGateway",
                "ec2:DetachInternetGateway",
                "ec2:DeleteInternetGateway",
                "ec2:AssociateRouteTable",
                "ec2:DisassociateRouteTable",
                "ec2:ReplaceRouteTableAssociation",
                "ec2:CreateRoute",
                "ec2:ReplaceRoute",
                "ec2:DeleteRoute",
                "ec2:DeleteRouteTable",
                "ec2:AuthorizeSecurityGroupIngress",
                "ec2:AuthorizeSecurityGroupEgress",
                "ec2:RevokeSecurityGroupIngress",
                "ec2:RevokeSecurityGroupEgress",
                "ec2:ModifySecurityGroupRules",
                "ec2:DeleteSecurityGroup",
                "ec2:ModifySubnetAttribute",
                "ec2:DeleteSubnet",
                "ec2:ModifyVpcAttribute",
                "ec2:DeleteVpc",
            ],
            [
                f"{ec2}:{kind}/*"
                for kind in (
                    "vpc",
                    "subnet",
                    "internet-gateway",
                    "route-table",
                    "security-group",
                    "security-group-rule",
                    "elastic-ip",
                    "instance",
                    "network-interface",
                )
            ],
            StringEquals=owned,
        ),
        allow(
            "TagDuringApprovedCreation",
            ["ec2:CreateTags"],
            [f"{ec2}:*"],
            StringEquals={
                **regional,
                "ec2:CreateAction": [action.split(":")[1] for action in network_create]
                + [
                    "RunInstances",
                    "CreateVolume",
                    "CreateNetworkInterface",
                    "ImportKeyPair",
                    "AuthorizeSecurityGroupIngress",
                    "AuthorizeSecurityGroupEgress",
                ],
            },
        ),
        allow(
            "UpdateOwnedTags",
            ["ec2:CreateTags", "ec2:DeleteTags"],
            [f"{ec2}:*"],
            StringEquals=owned,
            StringEqualsIfExists={"aws:RequestTag/Project": PROJECT},
        ),
        {
            "Sid": "PreserveOwnershipTag",
            "Effect": "Deny",
            "Action": "ec2:DeleteTags",
            "Resource": f"{ec2}:*",
            "Condition": {
                "ForAnyValue:StringEquals": {"aws:TagKeys": "Project"},
            },
        },
    )
    compute = policy(
        allow(
            "CreateTaggedVolumes",
            ["ec2:CreateVolume"],
            [f"{ec2}:volume/*"],
            StringEquals=create,
        ),
        allow(
            "CreateTaggedInterfaces",
            ["ec2:CreateNetworkInterface"],
            [f"{ec2}:network-interface/*"],
            StringEquals=create,
        ),
        allow(
            "UseOwnedInterfaceNetwork",
            ["ec2:CreateNetworkInterface"],
            [f"{ec2}:subnet/*", f"{ec2}:security-group/*"],
            StringEquals=owned,
        ),
        allow(
            "ImportTaggedPublicKey",
            ["ec2:ImportKeyPair"],
            [f"{ec2}:key-pair/klip-*"],
            StringEquals=create,
        ),
        allow(
            "LaunchTaggedInstancesAndVolumes",
            ["ec2:RunInstances"],
            [f"{ec2}:instance/*", f"{ec2}:volume/*"],
            StringEquals=create,
        ),
        allow(
            "LaunchWithOwnedNetworkAndKey",
            ["ec2:RunInstances"],
            [
                f"{ec2}:{kind}/*"
                for kind in (
                    "network-interface",
                    "subnet",
                    "security-group",
                    "key-pair",
                )
            ],
            StringEquals=owned,
        ),
        allow(
            "UseRegionalImages",
            ["ec2:RunInstances"],
            [f"arn:aws:ec2:{region}::image/*"],
            StringEquals=regional,
        ),
        allow(
            "ManageOwnedCompute",
            [
                "ec2:StartInstances",
                "ec2:StopInstances",
                "ec2:RebootInstances",
                "ec2:TerminateInstances",
                "ec2:ModifyInstanceAttribute",
                "ec2:ModifyInstanceMetadataOptions",
                "ec2:ModifyInstanceCreditSpecification",
                "ec2:DescribeInstanceAttribute",
                "ec2:GetConsoleOutput",
                "ec2:GetConsoleScreenshot",
                "ec2:AttachVolume",
                "ec2:DetachVolume",
                "ec2:DeleteVolume",
                "ec2:ModifyVolume",
                "ec2:AttachNetworkInterface",
                "ec2:DetachNetworkInterface",
                "ec2:DeleteNetworkInterface",
                "ec2:ModifyNetworkInterfaceAttribute",
                "ec2:AssociateIamInstanceProfile",
                "ec2:DisassociateIamInstanceProfile",
                "ec2:ReplaceIamInstanceProfileAssociation",
                "ec2:DeleteKeyPair",
            ],
            [
                f"{ec2}:{kind}/*"
                for kind in (
                    "instance",
                    "volume",
                    "network-interface",
                    "key-pair",
                )
            ],
            StringEquals=owned,
        ),
    )
    api = policy(
        allow(
            "ReadRegionalLoadBalancers",
            ["elasticloadbalancing:Describe*"],
            ["*"],
            StringEquals=regional,
        ),
        allow(
            "ManageNamedApiLoadBalancer",
            [
                f"elasticloadbalancing:{action}"
                for action in (
                    "CreateLoadBalancer",
                    "DeleteLoadBalancer",
                    "ModifyLoadBalancerAttributes",
                    "SetSecurityGroups",
                    "SetSubnets",
                    "SetIpAddressType",
                    "CreateTargetGroup",
                    "DeleteTargetGroup",
                    "ModifyTargetGroup",
                    "ModifyTargetGroupAttributes",
                    "RegisterTargets",
                    "DeregisterTargets",
                    "CreateListener",
                    "DeleteListener",
                    "ModifyListener",
                    "ModifyListenerAttributes",
                    "AddTags",
                    "RemoveTags",
                )
            ],
            [
                f"{elb}:{kind}"
                for kind in (
                    "loadbalancer/net/klip-*/*",
                    "targetgroup/klip-*/*",
                    "listener/net/klip-*/*/*",
                )
            ],
            StringEquals=regional,
        ),
        allow(
            "CreateOnlyElbServiceRole",
            ["iam:CreateServiceLinkedRole"],
            [
                f"{iam}:role/aws-service-role/elasticloadbalancing.amazonaws.com/AWSServiceRoleForElasticLoadBalancing"
            ],
            StringEquals={"iam:AWSServiceName": "elasticloadbalancing.amazonaws.com"},
        ),
    )
    identity_state = policy(
        allow(
            "ReadFoundationStateBucket",
            ["s3:ListBucket", "s3:GetBucketLocation"],
            [f"arn:aws:s3:::{state_bucket}"],
        ),
        allow("ReadWriteFoundationState", ["s3:GetObject", "s3:PutObject"], [state]),
        allow(
            "ManageFoundationStateLock",
            ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
            [f"{state}.tflock"],
        ),
        allow(
            "CreateBoundedNodeRole",
            ["iam:CreateRole", "iam:PutRolePermissionsBoundary"],
            [node_role],
            StringEquals={"iam:PermissionsBoundary": boundary},
        ),
        allow(
            "ManageOnlyNodeRole",
            [
                "iam:GetRole",
                "iam:DeleteRole",
                "iam:UpdateRole",
                "iam:UpdateAssumeRolePolicy",
                "iam:TagRole",
                "iam:UntagRole",
                "iam:ListRoleTags",
                "iam:ListAttachedRolePolicies",
                "iam:ListRolePolicies",
                "iam:GetRolePolicy",
                "iam:ListInstanceProfilesForRole",
            ],
            [node_role],
        ),
        allow(
            "AttachOnlySsmCore",
            ["iam:AttachRolePolicy", "iam:DetachRolePolicy"],
            [node_role],
            ArnEquals={"iam:PolicyARN": ssm_core},
        ),
        allow(
            "PassOnlyNodeRoleToEc2",
            ["iam:PassRole"],
            [node_role],
            StringEquals={"iam:PassedToService": "ec2.amazonaws.com"},
        ),
        allow(
            "ManageOnlyNodeProfile",
            [
                "iam:CreateInstanceProfile",
                "iam:GetInstanceProfile",
                "iam:DeleteInstanceProfile",
                "iam:AddRoleToInstanceProfile",
                "iam:RemoveRoleFromInstanceProfile",
                "iam:TagInstanceProfile",
                "iam:UntagInstanceProfile",
                "iam:ListInstanceProfileTags",
            ],
            [node_profile],
        ),
        allow(
            "ReadNodePolicyVersions",
            ["iam:GetPolicy", "iam:GetPolicyVersion"],
            [boundary, ssm_core],
        ),
        allow("IdentifySession", ["sts:GetCallerIdentity"], ["*"]),
    )
    node_boundary = policy(
        allow(
            "RegionalSsmAgentCore",
            [
                "ssm:DescribeAssociation",
                "ssm:GetDeployablePatchSnapshotForInstance",
                "ssm:GetDocument",
                "ssm:DescribeDocument",
                "ssm:GetManifest",
                "ssm:ListAssociations",
                "ssm:ListInstanceAssociations",
                "ssm:PutInventory",
                "ssm:PutComplianceItems",
                "ssm:PutConfigurePackageResult",
                "ssm:UpdateAssociationStatus",
                "ssm:UpdateInstanceAssociationStatus",
                "ssm:UpdateInstanceInformation",
                "ssmmessages:CreateControlChannel",
                "ssmmessages:CreateDataChannel",
                "ssmmessages:OpenControlChannel",
                "ssmmessages:OpenDataChannel",
                "ec2messages:AcknowledgeMessage",
                "ec2messages:DeleteMessage",
                "ec2messages:FailMessage",
                "ec2messages:GetEndpoint",
                "ec2messages:GetMessages",
                "ec2messages:SendReply",
            ],
            ["*"],
            StringEquals=regional,
        ),
        allow(
            "ReadOnlyAgentParameters",
            ["ssm:GetParameter", "ssm:GetParameters"],
            [
                f"arn:aws:ssm:{region}::parameter/aws/service/*",
                f"arn:aws:ssm:{region}:{account_id}:parameter/klip/*",
            ],
        ),
    )
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "OnlyProjectUserWithMfa",
                "Effect": "Allow",
                "Principal": {"AWS": f"{iam}:user/{source_user}"},
                "Action": "sts:AssumeRole",
                "Condition": {"Bool": {"aws:MultiFactorAuthPresent": "true"}},
            }
        ],
    }
    operations = policy(
        allow(
            "OperateOnlyProjectHosts",
            ["ssm:SendCommand", "ssm:StartSession"],
            [f"{ec2}:instance/*"],
            StringEquals={**regional, "ssm:resourceTag/Project": PROJECT},
        ),
        allow(
            "UseOnlyShellAndSshDocuments",
            ["ssm:SendCommand", "ssm:StartSession", "ssm:GetDocument", "ssm:DescribeDocument"],
            [
                f"arn:aws:ssm:{region}::document/{name}"
                for name in ("AWS-RunShellScript", "AWS-StartSSHSession")
            ],
            StringEquals=regional,
        ),
        allow(
            "ReadRegionalCommandResults",
            [
                "ssm:GetCommandInvocation",
                "ssm:ListCommandInvocations",
                "ssm:ListCommands",
                "ssm:CancelCommand",
                "ssm:GetConnectionStatus",
            ],
            ["*"],
            StringEquals=regional,
        ),
        allow(
            "ManageOwnOperatorSessions",
            ["ssm:ResumeSession", "ssm:TerminateSession", "ssmmessages:OpenDataChannel"],
            [f"arn:aws:ssm:{region}:{account_id}:session/klip-foundation-*"],
            StringEquals=regional,
        ),
    )
    dns_state = f"arn:aws:s3:::{state_bucket}/inference-dns/terraform.tfstate"
    dns = policy(
        allow(
            "ReadDnsStateBucket",
            ["s3:ListBucket", "s3:GetBucketLocation"],
            [f"arn:aws:s3:::{state_bucket}"],
        ),
        allow("ReadWriteDnsState", ["s3:GetObject", "s3:PutObject"], [dns_state]),
        allow(
            "ManageDnsStateLock",
            ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
            [f"{dns_state}.tflock"],
        ),
        allow(
            "InspectOnlyExistingZone",
            [
                "route53:GetHostedZone",
                "route53:ListResourceRecordSets",
                "route53:ListTagsForResource",
            ],
            [f"arn:aws:route53:::hostedzone/{hosted_zone_id}"],
        ),
        allow(
            "ChangeOnlyInferenceARecords",
            ["route53:ChangeResourceRecordSets"],
            [f"arn:aws:route53:::hostedzone/{hosted_zone_id}"],
            **{
                "ForAllValues:StringEquals": {
                    "route53:ChangeResourceRecordSetsNormalizedRecordNames": [inference_hostname],
                    "route53:ChangeResourceRecordSetsRecordTypes": ["A"],
                    "route53:ChangeResourceRecordSetsActions": ["CREATE", "UPSERT", "DELETE"],
                }
            },
        ),
        allow("ReadDnsChangeStatus", ["route53:GetChange"], ["arn:aws:route53:::change/*"]),
        # Route 53 does not support IAM resource/request-tag conditions. Creation
        # requires '*'; other health-check operations support only healthcheck ARNs.
        # Never pretend that ownership tags provide authorization for this service.
        allow("CreateHealthChecks", ["route53:CreateHealthCheck"], ["*"]),
        allow(
            "ReadHealthChecks",
            [
                "route53:GetHealthCheck",
                "route53:GetHealthCheckStatus",
                "route53:ListTagsForResource",
            ],
            ["arn:aws:route53:::healthcheck/*"],
        ),
        allow(
            "TagProjectHealthChecks",
            ["route53:ChangeTagsForResource"],
            ["arn:aws:route53:::healthcheck/*"],
        ),
        allow(
            "ManageProjectHealthChecks",
            ["route53:UpdateHealthCheck", "route53:DeleteHealthCheck"],
            ["arn:aws:route53:::healthcheck/*"],
        ),
        allow("IdentifySession", ["sts:GetCallerIdentity"], ["*"]),
    )
    return {
        "trust": trust,
        "network": network,
        "compute": compute,
        "api": api,
        "identity-state": identity_state,
        "node-boundary": node_boundary,
        "operations": operations,
        "dns": dns,
        "dns-trust": trust,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--source-user", default=PROJECT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    documents = build_documents(args.account_id, args.region, args.source_user)
    if not args.check:
        args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, document in documents.items():
        # IAM managed policies have a 6,144-character limit, excluding whitespace.
        if name != "trust" and len(json.dumps(document, separators=(",", ":"))) > 6144:
            raise ValueError(f"{name} exceeds the IAM managed-policy size limit")
        path = args.output_dir / f"{name}.json"
        content = json.dumps(document, indent=2) + "\n"
        if args.check:
            if not path.is_file() or path.read_text() != content:
                raise ValueError(f"Generated IAM policy drift: {path}")
        else:
            path.write_text(content)
    print(f"{'Verified' if args.check else 'Rendered'} {len(documents)} IAM documents")


if __name__ == "__main__":
    main()
