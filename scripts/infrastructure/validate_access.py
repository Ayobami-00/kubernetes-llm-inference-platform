"""Read-only AWS policy validation and explicit allow/deny simulations."""

from __future__ import annotations

import json

from aws_access import IAM_PATH, PROJECT, build_documents
from common import CONFIG, PROFILES, TERRAFORM, Aws, state_home, utc_now, write_private


def main() -> None:
    aws = Aws(PROFILES["bootstrap"])
    aws.preflight("bootstrap")
    documents = build_documents(CONFIG["account_id"], CONFIG["region"])
    folder = TERRAFORM / "state-bootstrap/policies"
    findings = {}
    for name in documents:
        arguments = [
            "accessanalyzer",
            "validate-policy",
            "--policy-document",
            (folder / f"{name}.json").as_uri(),
            "--policy-type",
            "RESOURCE_POLICY" if name.endswith("trust") else "IDENTITY_POLICY",
        ]
        if name.endswith("trust"):
            arguments += ["--validate-policy-resource-type", "AWS::IAM::AssumeRolePolicyDocument"]
        findings[name] = aws.call(*arguments)["findings"]
    write_private(state_home() / "reports/iam-findings.json", findings)
    account, region = CONFIG["account_id"], CONFIG["region"]
    node = f"arn:aws:iam::{account}:role{IAM_PATH}klip-control-plane"
    deployer = f"arn:aws:iam::{account}:role{IAM_PATH}kubernetes-llm-foundation-deployer"
    instance = f"arn:aws:ec2:{region}:{account}:instance/i-00000000000000001"
    state = f"arn:aws:s3:::{CONFIG['state_bucket']}"
    zone = f"arn:aws:route53:::hostedzone/{CONFIG['hosted_zone_id']}"
    checks = []

    def simulate(label, policies, action, resource, expected, context=None):
        arguments = [
            "iam",
            "simulate-custom-policy",
            "--policy-input-list",
            json.dumps([json.dumps(documents[name]) for name in policies]),
            "--action-names",
            action,
            "--resource-arns",
            resource,
        ]
        if context:
            arguments += [
                "--context-entries",
                json.dumps(
                    [
                        {"ContextKeyName": key, "ContextKeyValues": values, "ContextKeyType": kind}
                        for key, (kind, values) in context.items()
                    ]
                ),
            ]
        result = aws.call(*arguments)["EvaluationResults"][0]
        passed = (result["EvalDecision"] == "allowed") == expected
        checks.append({"name": label, "passed": passed, "evaluation": result})

    foundation = ["network", "compute", "api", "identity-state", "operations"]
    regional = {"aws:RequestedRegion": ("string", [region])}
    for action in ("ec2:CreateSubnet", "ec2:CreateRouteTable", "ec2:CreateSecurityGroup"):
        for project, expected in ((PROJECT, True), ("unrelated", False)):
            simulate(
                f"{action} parent VPC {project}",
                foundation,
                action,
                f"arn:aws:ec2:{region}:{account}:vpc/vpc-00000000000000001",
                expected,
                {**regional, "ec2:ResourceTag/Project": ("string", [project])},
            )
    for action in ("ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress"):
        for project, expected in ((PROJECT, True), ("unrelated", False)):
            simulate(
                f"{action} new rule {project}",
                foundation,
                action,
                f"arn:aws:ec2:{region}:{account}:security-group-rule/*",
                expected,
                {**regional, "aws:RequestTag/Project": ("string", [project])},
            )
            simulate(
                f"{action} parent group {project}",
                foundation,
                action,
                f"arn:aws:ec2:{region}:{account}:security-group/sg-00000000000000001",
                expected,
                {**regional, "ec2:ResourceTag/Project": ("string", [project])},
            )
        simulate(
            f"{action} untagged new rule denied",
            foundation,
            action,
            f"arn:aws:ec2:{region}:{account}:security-group-rule/*",
            False,
            regional,
        )
    simulate(
        "node passrole to EC2",
        foundation,
        "iam:PassRole",
        node,
        True,
        {"iam:PassedToService": ("string", ["ec2.amazonaws.com"])},
    )
    simulate(
        "node passrole to Lambda denied",
        foundation,
        "iam:PassRole",
        node,
        False,
        {"iam:PassedToService": ("string", ["lambda.amazonaws.com"])},
    )
    simulate("deployment role passrole denied", foundation, "iam:PassRole", deployer, False)
    simulate(
        "self trust mutation denied", foundation, "iam:UpdateAssumeRolePolicy", deployer, False
    )
    simulate("policy mutation denied", foundation, "iam:CreatePolicyVersion", "*", False)
    simulate("access key creation denied", foundation, "iam:CreateAccessKey", "*", False)
    simulate(
        "foundation state write",
        foundation,
        "s3:PutObject",
        state + "/aws-foundation/terraform.tfstate",
        True,
    )
    simulate(
        "bootstrap state write denied",
        foundation,
        "s3:PutObject",
        state + "/state-bootstrap/terraform.tfstate",
        False,
    )
    simulate(
        "state deletion denied",
        foundation,
        "s3:DeleteObject",
        state + "/aws-foundation/terraform.tfstate",
        False,
    )
    simulate(
        "project smoke allowed",
        foundation,
        "ssm:SendCommand",
        instance,
        True,
        {**regional, "ssm:resourceTag/Project": ("string", [PROJECT])},
    )
    simulate(
        "foreign host smoke denied",
        foundation,
        "ssm:SendCommand",
        instance,
        False,
        {**regional, "ssm:resourceTag/Project": ("string", ["unrelated"])},
    )
    simulate(
        "foundation DNS write denied", foundation, "route53:ChangeResourceRecordSets", zone, False
    )
    record_context = {
        "route53:ChangeResourceRecordSetsNormalizedRecordNames": (
            "stringList",
            [CONFIG["inference_hostname"]],
        ),
        "route53:ChangeResourceRecordSetsRecordTypes": ("stringList", ["A"]),
        "route53:ChangeResourceRecordSetsActions": ("stringList", ["UPSERT"]),
    }
    simulate(
        "inference A write", ["dns"], "route53:ChangeResourceRecordSets", zone, True, record_context
    )
    for label, key, value in [
        ("other DNS name denied", "NormalizedRecordNames", "other.klip-lab.click"),
        ("NS write denied", "RecordTypes", "NS"),
        ("TXT write denied", "RecordTypes", "TXT"),
    ]:
        context = {
            **record_context,
            f"route53:ChangeResourceRecordSets{key}": ("stringList", [value]),
        }
        simulate(label, ["dns"], "route53:ChangeResourceRecordSets", zone, False, context)
    simulate("zone deletion denied", ["dns"], "route53:DeleteHostedZone", zone, False)
    simulate(
        "DNS foundation state access denied",
        ["dns"],
        "s3:PutObject",
        state + "/aws-foundation/terraform.tfstate",
        False,
    )
    simulate(
        "DNS state write", ["dns"], "s3:PutObject", state + "/inference-dns/terraform.tfstate", True
    )
    report = {"observed_at": utc_now(), "findings": findings, "simulations": checks}
    write_private(state_home() / "reports/iam-validation.json", report)
    if any(findings.values()) or not all(item["passed"] for item in checks):
        raise ValueError("IAM validation failed; inspect private reports/iam-validation.json")
    print(f"Validated {len(findings)} documents without findings; {len(checks)} simulations passed")


if __name__ == "__main__":
    main()
