"""Security invariants for the AWS access bootstrap, without AWS credentials."""

import fnmatch
import json
import runpy
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "scripts/infrastructure/aws_access.py"
MODULE = runpy.run_path(str(SOURCE))
BUILD = MODULE["build_documents"]
ACCOUNT = "123456789012"


@pytest.fixture
def documents():
    return BUILD(ACCOUNT, "eu-west-2")


@pytest.mark.parametrize("account", ["", "123", "12345678901x", "1234567890123"])
def test_reject_invalid_account(account):
    with pytest.raises(ValueError, match="twelve digits"):
        BUILD(account, "eu-west-2")


def test_reject_unreviewed_region():
    with pytest.raises(ValueError, match="eu-west-2 only"):
        BUILD(ACCOUNT, "us-east-1")


@pytest.mark.parametrize("name", ["", "../admin", "some/user", "x" * 65])
def test_reject_invalid_source_identity(name):
    with pytest.raises(ValueError, match="IAM user name"):
        BUILD(ACCOUNT, "eu-west-2", name)


def test_trust_requires_exact_user_and_positive_mfa(documents):
    statements = documents["trust"]["Statement"]
    assert len(statements) == 1
    assert statements[0]["Principal"] == {
        "AWS": f"arn:aws:iam::{ACCOUNT}:user/kubernetes-llm-inference-platform"
    }
    assert statements[0]["Action"] == "sts:AssumeRole"
    assert statements[0]["Condition"] == {"Bool": {"aws:MultiFactorAuthPresent": "true"}}


def test_no_deployer_policy_or_credential_administration(documents):
    forbidden = [
        "iam:CreateAccessKey",
        "iam:CreateUser",
        "iam:CreatePolicy",
        "iam:CreatePolicyVersion",
        "iam:SetDefaultPolicyVersion",
        "iam:PutRolePolicy",
        "iam:PutUserPolicy",
        "iam:AttachUserPolicy",
        "iam:DeleteRolePermissionsBoundary",
        "route53:ChangeResourceRecordSets",
        "eks:CreateCluster",
        "lambda:CreateFunction",
    ]
    for name in ("network", "compute", "api", "identity-state", "operations"):
        for statement in documents[name]["Statement"]:
            if statement["Effect"] == "Allow":
                for action in statement["Action"]:
                    assert not any(
                        fnmatch.fnmatchcase(item.lower(), action.lower()) for item in forbidden
                    )


def test_iam_writes_cannot_target_the_deployment_role(documents):
    for name in ("network", "compute", "api", "identity-state"):
        for statement in documents[name]["Statement"]:
            actions = statement["Action"]
            if isinstance(actions, str):
                actions = [actions]
            if any(action.startswith("iam:") for action in actions):
                for resource in statement["Resource"]:
                    assert resource != "*"
                    assert "foundation-deployer" not in resource


def test_role_creation_and_passrole_cannot_escape_node_boundary(documents):
    statements = documents["identity-state"]["Statement"]
    create = next(item for item in statements if item["Sid"] == "CreateBoundedNodeRole")
    assert create["Resource"] == [
        f"arn:aws:iam::{ACCOUNT}:role/kubernetes-llm-inference-platform/klip-control-plane",
        f"arn:aws:iam::{ACCOUNT}:role/kubernetes-llm-inference-platform/klip-network-probe",
        f"arn:aws:iam::{ACCOUNT}:role/kubernetes-llm-inference-platform/klip-platform-worker",
    ]
    assert create["Condition"]["StringEquals"]["iam:PermissionsBoundary"].endswith(
        "/kubernetes-llm-control-plane-boundary"
    )
    pass_role = next(item for item in statements if item["Sid"] == "PassOnlyNodeRoleToEc2")
    assert pass_role["Resource"] == create["Resource"]
    assert pass_role["Condition"] == {"StringEquals": {"iam:PassedToService": "ec2.amazonaws.com"}}


def test_backup_grant_has_instance_specific_write_only_prefix(documents):
    grant = documents["node-backup"]["Statement"]
    assert len(grant) == 1
    assert grant[0]["Action"] == ["s3:PutObject"]
    assert grant[0]["Resource"] == [
        f"arn:aws:s3:::klip-backups-{ACCOUNT}-eu-west-2/clusters/klip/${{aws:userid}}/*"
    ]
    attach = next(
        item
        for item in documents["identity-state"]["Statement"]
        if item["Sid"] == "AttachOnlyNodeBackup"
    )
    assert len(attach["Resource"]) == 1
    assert attach["Resource"][0].endswith("/klip-control-plane")


def test_state_deletion_is_limited_to_foundation_lock(documents):
    for statement in documents["identity-state"]["Statement"]:
        if "s3:DeleteObject" in statement["Action"]:
            assert statement["Resource"] == [
                f"arn:aws:s3:::klip-tfstate-{ACCOUNT}-eu-west-2/aws-foundation/terraform.tfstate.tflock"
            ]


def test_existing_network_and_compute_require_ownership(documents):
    for name, sid in (("network", "ManageOwnedNetwork"), ("compute", "ManageOwnedCompute")):
        statement = next(item for item in documents[name]["Statement"] if item["Sid"] == sid)
        assert (
            statement["Condition"]["StringEquals"]["ec2:ResourceTag/Project"] == MODULE["PROJECT"]
        )
        assert statement["Condition"]["StringEquals"]["aws:RequestedRegion"] == "eu-west-2"


def test_managed_policies_fit_aws_size_limit(documents):
    for name, document in documents.items():
        if name != "trust":
            assert len(json.dumps(document, separators=(",", ":"))) <= 6144


def test_cpu_options_are_granted_only_to_owned_lab_platform_workers(documents):
    permission = next(
        s for s in documents["compute"]["Statement"] if s["Sid"] == "ResizeOwnedPlatformCpu"
    )
    assert permission["Action"] == ["ec2:ModifyInstanceCpuOptions"]
    conditions = permission["Condition"]["StringEquals"]
    assert conditions["ec2:ResourceTag/Node"] == ["platform-01", "platform-02", "platform-03"]
    assert conditions["ec2:ResourceTag/Cluster"] == "klip"
    assert conditions["ec2:ResourceTag/Environment"] == "lab"
    assert conditions["aws:RequestedRegion"] == "eu-west-2"
    assert "ModifyInstanceCpuOptions" not in json.dumps(documents["node-boundary"])


def test_dns_has_exact_record_scope_and_separate_state(documents):
    dns = {item["Sid"]: item for item in documents["dns"]["Statement"]}
    record = dns["ChangeOnlyInferenceARecords"]
    conditions = record["Condition"]["ForAllValues:StringEquals"]
    assert conditions["route53:ChangeResourceRecordSetsNormalizedRecordNames"] == [
        "inference.klip-lab.click"
    ]
    assert conditions["route53:ChangeResourceRecordSetsRecordTypes"] == ["A"]
    assert dns["ReadWriteDnsState"]["Resource"] == [
        f"arn:aws:s3:::klip-tfstate-{ACCOUNT}-eu-west-2/inference-dns/terraform.tfstate"
    ]
    assert documents["dns-trust"] == documents["trust"]


def test_health_check_permissions_do_not_claim_unsupported_tag_authorization(documents):
    for item in documents["dns"]["Statement"]:
        if item["Sid"] == "InspectOnlyExistingZone":
            continue
        if any("HealthCheck" in action or "TagsForResource" in action for action in item["Action"]):
            assert "Condition" not in item
            if "route53:CreateHealthCheck" not in item["Action"]:
                assert item["Resource"] == ["arn:aws:route53:::healthcheck/*"]


def test_smoke_requires_project_owned_instances(documents):
    operation = documents["operations"]["Statement"][0]
    assert operation["Condition"]["StringEquals"]["ssm:resourceTag/Project"] == MODULE["PROJECT"]
    assert operation["Resource"] == [f"arn:aws:ec2:eu-west-2:{ACCOUNT}:instance/*"]


def test_network_creation_parent_permissions_require_owned_vpc(documents):
    statement = next(
        s for s in documents["network"]["Statement"] if s["Sid"] == "CreateWithinOwnedVpc"
    )
    assert set(statement["Action"]) == {
        "ec2:CreateSubnet",
        "ec2:CreateRouteTable",
        "ec2:CreateSecurityGroup",
    }
    assert statement["Resource"] == [f"arn:aws:ec2:eu-west-2:{ACCOUNT}:vpc/*"]
    assert statement["Condition"]["StringEquals"]["ec2:ResourceTag/Project"] == MODULE["PROJECT"]


def test_new_rules_require_request_tags_and_owned_parent_groups(documents):
    statement = next(
        s for s in documents["network"]["Statement"] if s["Sid"] == "CreateTaggedSecurityGroupRules"
    )
    assert set(statement["Action"]) == {
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:AuthorizeSecurityGroupEgress",
    }
    assert statement["Resource"] == [f"arn:aws:ec2:eu-west-2:{ACCOUNT}:security-group-rule/*"]
    assert statement["Condition"]["StringEquals"]["aws:RequestTag/Project"] == MODULE["PROJECT"]
    parent = next(s for s in documents["network"]["Statement"] if s["Sid"] == "ManageOwnedNetwork")
    assert parent["Condition"]["StringEquals"]["ec2:ResourceTag/Project"] == MODULE["PROJECT"]
