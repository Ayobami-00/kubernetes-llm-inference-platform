"""Read back retained repositories and exact workload/CI publishing identities."""

from __future__ import annotations

import json

from common import CONFIG, PROFILES, Aws, source_digest, state_home, tf, utc_now, write_private
from platform_storage import assert_policy


def verify() -> dict:
    aws = Aws(PROFILES["registry"])
    aws.preflight("registry")
    allocation = json.loads(tf("registry", "output", "-json", "registry_manifest"))
    if any(
        allocation.get(k) != CONFIG[k] for k in ("account_id", "region", "project", "cluster_id")
    ):
        raise ValueError("Unexpected registry allocation identity")
    state = {
        x["address"]: x["values"]
        for x in json.loads(tf("registry", "show", "-json"))["values"]["root_module"]["resources"]
    }
    names = [x["name"] for x in allocation["repositories"].values()]
    if sorted(names) != ["klip/management-backend", "klip/management-web", "klip/workload-runner"]:
        raise ValueError("Unexpected registry repository scope")
    actual = aws.call("ecr", "describe-repositories", "--repository-names", *names)["repositories"]
    if {x["repositoryName"] for x in actual} != set(names):
        raise ValueError("Repository read-back does not match the allocation")
    observed = {}
    publisher = allocation["publisher"]
    provider = aws.call(
        "iam",
        "get-open-id-connect-provider",
        "--open-id-connect-provider-arn",
        publisher["provider_arn"],
    )
    if (
        provider["Url"] != "token.actions.githubusercontent.com"
        or provider["ClientIDList"] != ["sts.amazonaws.com"]
        or publisher["repository"] != "Ayobami-00/kubernetes-llm-inference-platform"
        or publisher["owner_id"] != "41403457"
        or publisher["repository_id"] != "1432746028"
    ):
        raise ValueError("CI publisher issuer/audience/immutable repository identity differs")
    for repo in actual:
        name = repo["repositoryName"]
        key = name.split("/", 1)[1]
        expected = allocation["repositories"][key]
        if (
            repo["registryId"] != CONFIG["account_id"]
            or repo["repositoryArn"] != expected["arn"]
            or repo["repositoryUri"] != expected["url"]
            or repo["imageTagMutability"] != "IMMUTABLE"
            or repo["encryptionConfiguration"]["encryptionType"] != "AES256"
            or repo["imageScanningConfiguration"].get("scanOnPush") is not True
        ):
            raise ValueError("Repository encryption, immutable tags or scan settings differ")
        tags = {
            x["Key"]: x["Value"]
            for x in aws.call("ecr", "list-tags-for-resource", "--resource-arn", expected["arn"])[
                "tags"
            ]
        }
        if any(
            tags.get(k) != v
            for k, v in {
                "Project": CONFIG["project"],
                "Cluster": CONFIG["cluster_id"],
                "Environment": CONFIG["environment"],
                "ManagedBy": "terraform-platform-registry",
            }.items()
        ):
            raise ValueError("Registry ownership tags differ from the reviewed project")
        lifecycle = aws.call("ecr", "get-lifecycle-policy", "--repository-name", name)
        assert_policy(
            lifecycle["lifecyclePolicyText"],
            state['aws_ecr_lifecycle_policy.untagged["' + key + '"]']["policy"],
            "image lifecycle",
        )
        try:
            aws.call("ecr", "get-repository-policy", "--repository-name", name)
        except RuntimeError as error:
            if "RepositoryPolicyNotFoundException" not in str(error):
                raise
        else:
            raise ValueError(
                "An unexpected repository policy may grant access outside workload IAM"
            )
        observed[key] = {
            "name": name,
            "url": expected["url"],
            "immutable": True,
            "encrypted": True,
            "scan_on_push": True,
            "resource_policy_absent": True,
        }
    for key, declared in allocation["roles"].items():
        role = aws.call("iam", "get-role", "--role-name", declared["name"])["Role"]
        if role["Arn"] != declared["arn"] or role["MaxSessionDuration"] != 3600:
            raise ValueError("Registry role identity or maximum session differs")
        assert_policy(
            role["AssumeRolePolicyDocument"],
            state['aws_iam_role.images["' + key + '"]']["assume_role_policy"],
            "image role trust",
        )
        if aws.call("iam", "list-attached-role-policies", "--role-name", declared["name"])[
            "AttachedPolicies"
        ]:
            raise ValueError("Registry workload role has an unexpected managed policy")
        expected = state['aws_iam_role_policy.images["' + key + '"]']
        if aws.call("iam", "list-role-policies", "--role-name", declared["name"])[
            "PolicyNames"
        ] != [expected["name"]]:
            raise ValueError("Registry workload role has additional inline policies")
        actual_policy = aws.call(
            "iam",
            "get-role-policy",
            "--role-name",
            declared["name"],
            "--policy-name",
            expected["name"],
        )["PolicyDocument"]
        assert_policy(actual_policy, expected["policy"], "image workload permissions")
    result = {
        "schema_version": "klip-registry-observation.v1",
        "observed_at": utc_now(),
        "source_sha256": source_digest(),
        "allocation": allocation,
        "repositories": observed,
        "not_covered_by_this_check": [
            "kubelet credential-provider rollout",
            "workload image pulls",
            "image publication/scanning results",
            "deployed application availability",
        ],
    }
    write_private(state_home() / "reports/platform-registry.json", result)
    return result


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
