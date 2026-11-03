locals {
  config          = jsondecode(file("${path.module}/../../project.json"))
  identity        = data.terraform_remote_state.identity.outputs.identity_manifest
  images          = toset(["management-backend", "management-web", "workload-runner"])
  repository_arns = [for name in sort(tolist(local.images)) : "arn:aws:ecr:${local.config.region}:${local.config.account_id}:repository/${local.config.cluster_id}/${name}"]
  subjects = {
    pull = [for name in ["klip-api", "klip-worker", "klip-web", "klip-migrate", "klip-runner", "klip-image-pull-probe"] : "system:serviceaccount:klip:${name}"]
    push = ["system:serviceaccount:platform-build:image-publisher"]
  }
  tags = {
    Project     = local.config.project, Cluster = local.config.cluster_id,
    Environment = local.config.environment, ManagedBy = "terraform-platform-registry"
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

# The kubelet exchanges a Pod-bound token; hosts receive no ECR policy or static key.
resource "aws_iam_role" "images" {
  for_each             = local.subjects
  name                 = "klip-images-${each.key}"
  path                 = "/${local.config.project}/"
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = local.identity.provider_arn },
    Condition = { StringEquals = {
      "oidc.${local.config.domain}:sub" = each.value,
      "oidc.${local.config.domain}:aud" = "sts.amazonaws.com"
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
      each.key == "push" ? ["ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage"] : []),
      Resource = local.repository_arns
    }
  ] })
}

output "registry_manifest" {
  value = {
    schema_version = "klip-platform-registry.v1", project = local.config.project,
    cluster_id     = local.config.cluster_id, account_id = local.config.account_id,
    region         = local.config.region,
    repositories = { for name, repository in aws_ecr_repository.images : name => {
      name = repository.name, arn = repository.arn, url = repository.repository_url
    } },
    roles = { for name, role in aws_iam_role.images : name => {
      arn = role.arn, name = role.name, subjects = local.subjects[name]
    } }
  }
}
