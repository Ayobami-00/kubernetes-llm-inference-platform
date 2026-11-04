mock_provider "aws" {}
override_resource {
  override_during = plan
  target          = aws_iam_openid_connect_provider.github
  values          = { arn = "arn:aws:iam::127811062476:oidc-provider/token.actions.githubusercontent.com" }
}
override_data {
  target = data.terraform_remote_state.identity
  values = {
    outputs = {
      identity_manifest = {
        account_id   = "127811062476", region = "eu-west-2", cluster_id = "klip",
        project      = "kubernetes-llm-inference-platform", issuer = "https://oidc.klip-lab.click",
        audience     = "sts.amazonaws.com",
        provider_arn = "arn:aws:iam::127811062476:oidc-provider/oidc.klip-lab.click"
      }
    }
  }
}

run "private_immutable_project_images" {
  command = plan
  assert {
    condition     = length(aws_ecr_repository.images) == 4 && alltrue([for repository in aws_ecr_repository.images : repository.image_tag_mutability == "IMMUTABLE" && !repository.force_delete && repository.encryption_configuration[0].encryption_type == "AES256"])
    error_message = "Preserve private encrypted immutable project images through retained-state rebuild."
  }
  assert {
    condition     = local.subjects.model-pull == ["system:serviceaccount:llm-d:klip-model-release"] && !contains(local.role_repositories.pull, local.repository_arns["model-serving"]) && length(local.role_repositories.model-pull) == 2 && !contains(jsondecode(aws_iam_role_policy.images["model-pull"].policy).Statement[1].Action, "ecr:PutImage") && jsondecode(aws_iam_role.images["model-pull"].assume_role_policy).Statement[0].Condition.StringEquals["oidc.klip-lab.click:sub"] == local.subjects.model-pull
    error_message = "Model-release pull authority must use its exact cluster subject, remain read-only and not expand application image access."
  }
  assert {
    condition     = alltrue([for policy in aws_ecr_lifecycle_policy.untagged : jsondecode(policy.policy).rules[0].selection.tagStatus == "untagged"])
    error_message = "Automatic retention must not remove tagged historical run images."
  }
  assert {
    condition     = length(local.subjects.pull) == 8 && contains(local.subjects.pull, "system:serviceaccount:platform-ops:klip-host-api") && contains(local.subjects.pull, "system:serviceaccount:platform-ops:klip-host-job") && local.subjects.push == ["repo:Ayobami-00@41403457/kubernetes-llm-inference-platform@1432746028:ref:refs/heads/codex/end-to-end-platform"] && aws_iam_role.images["pull"].max_session_duration == 3600 && !contains(jsondecode(aws_iam_role_policy.images["pull"].policy).Statement[1].Action, "ecr:PutImage")
    error_message = "Image pull and publisher credentials need distinct exact workload subjects and permissions."
  }
  assert {
    condition     = aws_iam_openid_connect_provider.github.url == "https://token.actions.githubusercontent.com" && aws_iam_openid_connect_provider.github.client_id_list == toset(["sts.amazonaws.com"]) && jsondecode(aws_iam_role.images["push"].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == local.subjects.push
    error_message = "CI write access must be limited to the reviewed immutable repository/branch subject."
  }
}

run "reject_foreign_workload_identity" {
  command = plan
  override_data {
    target = data.terraform_remote_state.identity
    values = {
      outputs = {
        identity_manifest = {
          account_id   = "000000000000", region = "eu-west-2", cluster_id = "klip",
          project      = "kubernetes-llm-inference-platform", issuer = "https://oidc.klip-lab.click",
          audience     = "sts.amazonaws.com",
          provider_arn = "arn:aws:iam::000000000000:oidc-provider/oidc.klip-lab.click"
        }
      }
    }
  }
  expect_failures = [terraform_data.identity_guard]
}
