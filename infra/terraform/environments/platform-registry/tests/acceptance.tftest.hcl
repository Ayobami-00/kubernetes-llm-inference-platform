mock_provider "aws" {}
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
    condition     = length(aws_ecr_repository.images) == 3 && alltrue([for repository in aws_ecr_repository.images : repository.image_tag_mutability == "IMMUTABLE" && !repository.force_delete && repository.encryption_configuration[0].encryption_type == "AES256"])
    error_message = "Preserve private encrypted immutable project images through retained-state rebuild."
  }
  assert {
    condition     = alltrue([for policy in aws_ecr_lifecycle_policy.untagged : jsondecode(policy.policy).rules[0].selection.tagStatus == "untagged"])
    error_message = "Automatic retention must not remove tagged historical run images."
  }
  assert {
    condition     = length(local.subjects.pull) == 6 && local.subjects.push == ["system:serviceaccount:platform-build:image-publisher"] && aws_iam_role.images["pull"].max_session_duration == 3600 && !contains(jsondecode(aws_iam_role_policy.images["pull"].policy).Statement[1].Action, "ecr:PutImage")
    error_message = "Image pull and publisher credentials need distinct exact workload subjects and permissions."
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
