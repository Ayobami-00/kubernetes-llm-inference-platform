locals {
  config             = jsondecode(file("${path.module}/../../project.json"))
  github             = jsondecode(file("${path.module}/../../registry.json")).github
  identity           = data.terraform_remote_state.identity.outputs.identity_manifest
  application_images = toset(["management-backend", "management-web", "workload-runner"])
  images             = setunion(local.application_images, toset(["model-serving"]))
  repository_arns    = { for name in local.images : name => "arn:aws:ecr:${local.config.region}:${local.config.account_id}:repository/${local.config.cluster_id}/${name}" }
  subjects = {
    pull = concat(
      [for name in ["klip-api", "klip-worker", "klip-web", "klip-migrate", "klip-runner", "klip-image-pull-probe"] : "system:serviceaccount:klip:${name}"],
      [for name in ["klip-host-api", "klip-host-job"] : "system:serviceaccount:platform-ops:${name}"]
    )
    model-pull = ["system:serviceaccount:llm-d:klip-model-release"]
    push       = [for ref in local.github.refs : "repo:${split("/", local.github.repository)[0]}@${local.github.owner_id}/${split("/", local.github.repository)[1]}@${local.github.repository_id}:ref:${ref}"]
  }
  role_repositories = {
    pull       = [for name in sort(tolist(local.application_images)) : local.repository_arns[name]]
    model-pull = [local.repository_arns["management-backend"], local.repository_arns["model-serving"]]
    push       = [for name in sort(tolist(local.images)) : local.repository_arns[name]]
  }
  tags = {
    Project     = local.config.project, Cluster = local.config.cluster_id,
    Environment = local.config.environment, ManagedBy = "terraform-platform-registry"
  }
}

# This account had no GitHub provider at adoption. Retain it through lab rebuilds;
# an existing shared provider must be explicitly adopted, never replaced.
resource "aws_iam_openid_connect_provider" "github" {
  url            = local.github.issuer
  client_id_list = ["sts.amazonaws.com"]
  tags           = local.tags
  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = local.github.issuer == "https://token.actions.githubusercontent.com" && local.github.repository == "Ayobami-00/kubernetes-llm-inference-platform" && local.github.owner_id == "41403457" && local.github.repository_id == "1432746028" && length(local.github.refs) > 0 && alltrue([for ref in local.github.refs : contains(["refs/heads/codex/end-to-end-platform", "refs/heads/main"], ref)])
      error_message = "Publishing trust must use the verified immutable repository identity and explicitly reviewed branches."
    }
  }
}

provider "aws" {
  region              = local.config.region
  allowed_account_ids = [local.config.account_id]
}

data "terraform_remote_state" "identity" {
  backend = "s3"
  config = {
    bucket = local.config.state_bucket, key = "workload-identity/terraform.tfstate",
    region = local.config.region, encrypt = true
  }
}

resource "terraform_data" "identity_guard" {
  input = local.identity.provider_arn
  lifecycle {
    precondition {
      condition     = local.identity.account_id == local.config.account_id && local.identity.region == local.config.region && local.identity.cluster_id == local.config.cluster_id && local.identity.project == local.config.project && local.identity.issuer == "https://oidc.${local.config.domain}" && local.identity.audience == "sts.amazonaws.com" && local.identity.provider_arn == "arn:aws:iam::${local.config.account_id}:oidc-provider/oidc.${local.config.domain}"
      error_message = "Registry credentials require this project's verified retained OIDC identity."
    }
  }
}

# Retain tagged, digest-addressed software needed by historical runs and rebuilds.
resource "aws_ecr_repository" "images" {
  for_each             = local.images
  name                 = "${local.config.cluster_id}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  encryption_configuration { encryption_type = "AES256" }
  image_scanning_configuration { scan_on_push = true }
  tags = local.tags
  lifecycle { prevent_destroy = true }
}

resource "aws_ecr_lifecycle_policy" "untagged" {
  for_each   = aws_ecr_repository.images
  repository = each.value.name
  policy = jsonencode({ rules = [{
    rulePriority = 1, description = "Only untagged images after 30 days; retain tagged run pins",
    selection    = { tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 30 },
    action       = { type = "expire" }
  }] })
}

# Pull uses Pod-bound tokens; publishing uses the verified repository's CI tokens.
# Neither the hosts nor application Pods receive ECR write permissions.
resource "aws_iam_role" "images" {
  for_each             = local.subjects
  name                 = "klip-images-${each.key}"
  path                 = "/${local.config.project}/"
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = each.key != "push" ? local.identity.provider_arn : aws_iam_openid_connect_provider.github.arn },
    Condition = { StringEquals = {
      "${each.key != "push" ? "oidc.${local.config.domain}" : "token.actions.githubusercontent.com"}:sub" = each.value,
      "${each.key != "push" ? "oidc.${local.config.domain}" : "token.actions.githubusercontent.com"}:aud" = "sts.amazonaws.com"
    } }
  }] })
  tags       = local.tags
  depends_on = [terraform_data.identity_guard]
}

resource "aws_iam_role_policy" "images" {
  for_each = local.subjects
  name     = "project-images-${each.key}"
  role     = aws_iam_role.images[each.key].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Effect    = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*",
      Condition = { StringEquals = { "aws:RequestedRegion" = local.config.region } }
    },
    {
      Effect = "Allow",
      Action = concat(["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
      each.key == "push" ? ["ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:DescribeImages", "ecr:DescribeImageScanFindings"] : []),
      Resource = local.role_repositories[each.key]
    }
  ] })
}

output "registry_manifest" {
  value = {
    schema_version = "klip-platform-registry.v1", project = local.config.project,
    cluster_id     = local.config.cluster_id, account_id = local.config.account_id,
    region         = local.config.region,
    publisher = {
      issuer        = local.github.issuer, provider_arn = aws_iam_openid_connect_provider.github.arn,
      repository    = local.github.repository, owner_id = local.github.owner_id,
      repository_id = local.github.repository_id, refs = local.github.refs
    },
    repositories = { for name, repository in aws_ecr_repository.images : name => {
      name = repository.name, arn = repository.arn, url = repository.repository_url
    } },
    roles = { for name, role in aws_iam_role.images : name => {
      arn = role.arn, name = role.name, subjects = local.subjects[name]
    } }
  }
}
