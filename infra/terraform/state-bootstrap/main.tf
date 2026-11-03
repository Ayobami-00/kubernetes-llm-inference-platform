locals {
  config = jsondecode(file("${path.module}/../project.json"))
  tags = {
    Project     = local.config.project, Cluster = local.config.cluster_id,
    Environment = local.config.environment, ManagedBy = "terraform-state-bootstrap"
  }
  iam_path = "/${local.config.project}/"
  policy_names = {
    network        = "kubernetes-llm-foundation-network"
    compute        = "kubernetes-llm-foundation-compute"
    api            = "kubernetes-llm-foundation-api"
    identity-state = "kubernetes-llm-foundation-identity-state"
    operations     = "kubernetes-llm-foundation-operations"
    node-boundary  = "kubernetes-llm-control-plane-boundary"
    dns            = "kubernetes-llm-inference-dns"
  }
  existing_policies    = toset(["network", "compute", "api", "identity-state", "node-boundary"])
  foundation_policies  = toset(["network", "compute", "api", "identity-state", "operations"])
  existing_attachments = toset(["network", "compute", "api", "identity-state"])
}

variable "adopt_existing_access" {
  description = "Always true for live bootstrap; false only in credential-free provider-mock tests."
  type        = bool
  default     = true
}

resource "aws_s3_bucket" "state" {
  bucket        = local.config.state_bucket
  force_destroy = false
  tags          = local.tags
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "s3:*",
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"],
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_iam_policy" "access" {
  for_each = local.policy_names
  name     = each.value
  path     = local.iam_path
  policy   = file("${path.module}/policies/${each.key}.json")
  tags     = local.tags
}

resource "aws_iam_role" "foundation" {
  name                 = "kubernetes-llm-foundation-deployer"
  path                 = local.iam_path
  description          = "MFA-protected, project-scoped AWS foundation deployment"
  max_session_duration = 3600
  assume_role_policy   = file("${path.module}/policies/trust.json")
  tags                 = local.tags
}

resource "aws_iam_role_policy_attachment" "foundation" {
  for_each   = local.foundation_policies
  role       = aws_iam_role.foundation.name
  policy_arn = aws_iam_policy.access[each.key].arn
}

resource "aws_iam_role" "dns" {
  name                 = "kubernetes-llm-inference-dns-deployer"
  path                 = local.iam_path
  description          = "MFA-protected inference records and health checks; no compute permissions"
  max_session_duration = 3600
  assume_role_policy   = file("${path.module}/policies/dns-trust.json")
  tags                 = local.tags
}

resource "aws_iam_role_policy_attachment" "dns" {
  role       = aws_iam_role.dns.name
  policy_arn = aws_iam_policy.access["dns"].arn
}

# Existing access objects are adopted, never recreated or temporarily detached.
import {
  for_each = var.adopt_existing_access ? toset(["kubernetes-llm-foundation-deployer"]) : toset([])
  to       = aws_iam_role.foundation
  id       = each.value
}
import {
  for_each = var.adopt_existing_access ? local.existing_policies : toset([])
  to       = aws_iam_policy.access[each.key]
  id       = "arn:aws:iam::${local.config.account_id}:policy${local.iam_path}${local.policy_names[each.key]}"
}
import {
  for_each = var.adopt_existing_access ? local.existing_attachments : toset([])
  to       = aws_iam_role_policy_attachment.foundation[each.key]
  id       = "kubernetes-llm-foundation-deployer/arn:aws:iam::${local.config.account_id}:policy${local.iam_path}${local.policy_names[each.key]}"
}
