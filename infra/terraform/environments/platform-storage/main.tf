locals {
  config   = jsondecode(file("${path.module}/../../project.json"))
  storage  = jsondecode(file("${path.module}/../../storage.json"))
  identity = data.terraform_remote_state.identity.outputs.identity_manifest
  tags = {
    Project     = local.config.project, Cluster = local.config.cluster_id,
    Environment = local.config.environment, ManagedBy = "terraform-platform-storage"
  }
  subjects = {
    artifacts = [local.storage.efs_node_service_account]
    database  = [for name in local.storage.database.service_accounts : "system:serviceaccount:${local.storage.database.namespace}:${name}"]
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
      error_message = "Storage requires this project's verified retained OIDC provider."
    }
    precondition {
      condition     = local.storage.schema_version == "klip-platform-storage.v1" && local.storage.artifacts.uid == 10001 && local.storage.artifacts.gid == 10001 && local.storage.artifacts.path == "/artifacts" && local.storage.database.noncurrent_retention_days > local.storage.database.recovery_window_days
      error_message = "Preserve non-root artifact ownership and a version-recovery buffer beyond the database recovery window."
    }
  }
}

# These roles are workload-only. No instance profile or management API receives
# infrastructure credentials. Both namespace and account names are exact subjects.
resource "aws_iam_role" "storage" {
  for_each             = local.subjects
  name                 = "klip-storage-${each.key}"
  path                 = "/${local.config.project}/"
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = local.identity.provider_arn },
    Condition = { StringEquals = {
      "oidc.${local.config.domain}:sub" = each.value,
      "oidc.${local.config.domain}:aud" = local.identity.audience
    } }
  }] })
  tags       = local.tags
  depends_on = [terraform_data.identity_guard]
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket" "database" {
  bucket        = "klip-database-${local.config.account_id}-${local.config.region}"
  force_destroy = false
  tags          = local.tags
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket_public_access_block" "database" {
  bucket                  = aws_s3_bucket.database.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_ownership_controls" "database" {
  bucket = aws_s3_bucket.database.id
  rule { object_ownership = "BucketOwnerEnforced" }
}
resource "aws_s3_bucket_versioning" "database" {
  bucket = aws_s3_bucket.database.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "database" {
  bucket = aws_s3_bucket.database.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_lifecycle_configuration" "database" {
  bucket = aws_s3_bucket.database.id
  rule {
    id     = "retain-recovery-versions"
    status = "Enabled"
    filter { prefix = "${local.storage.database.archive_prefix}/" }
    # Barman owns current backup/WAL expiry. Never age out current WAL independently.
    noncurrent_version_expiration { noncurrent_days = local.storage.database.noncurrent_retention_days }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
  depends_on = [aws_s3_bucket_versioning.database]
}
resource "aws_s3_bucket_policy" "database" {
  bucket = aws_s3_bucket.database.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Sid       = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "s3:*",
    Resource  = [aws_s3_bucket.database.arn, "${aws_s3_bucket.database.arn}/*"],
    Condition = { Bool = { "aws:SecureTransport" = "false" } }
  }] })
}
resource "aws_iam_role_policy" "database" {
  name = "database-archive-only"
  role = aws_iam_role.storage["database"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetBucketLocation", "s3:ListBucket"], Resource = aws_s3_bucket.database.arn },
    {
      Effect    = "Allow", Action = ["s3:ListBucketMultipartUploads"], Resource = aws_s3_bucket.database.arn,
      Condition = { StringLike = { "s3:prefix" = ["${local.storage.database.archive_prefix}/*"] } }
    },
    {
      Effect   = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
      Resource = "${aws_s3_bucket.database.arn}/${local.storage.database.archive_prefix}/*"
    }
  ] })
}

resource "aws_efs_file_system" "artifacts" {
  creation_token   = "klip-retained-artifacts"
  encrypted        = true
  performance_mode = "generalPurpose"
  throughput_mode  = local.storage.artifacts.throughput_mode
  tags             = merge(local.tags, { Name = "klip-artifacts" })
  # Regional storage, independent of the replaceable VPC and its mount targets.
  lifecycle { prevent_destroy = true }
}
resource "aws_efs_access_point" "artifacts" {
  file_system_id = aws_efs_file_system.artifacts.id
  posix_user {
    uid = local.storage.artifacts.uid
    gid = local.storage.artifacts.gid
  }
  root_directory {
    path = local.storage.artifacts.path
    creation_info {
      owner_uid   = local.storage.artifacts.uid
      owner_gid   = local.storage.artifacts.gid
      permissions = local.storage.artifacts.permissions
    }
  }
  tags = local.tags
  lifecycle { prevent_destroy = true }
}
resource "aws_efs_backup_policy" "artifacts" {
  file_system_id = aws_efs_file_system.artifacts.id
  # AWS's default EFS plan: daily, 35 days. Verify successful recovery points and restore.
  backup_policy { status = "ENABLED" }
}
resource "aws_iam_role_policy" "artifacts" {
  name = "artifact-access-point-only"
  role = aws_iam_role.storage["artifacts"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow", Action = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"],
    Resource = aws_efs_file_system.artifacts.arn,
    Condition = {
      StringEquals = { "elasticfilesystem:AccessPointArn" = aws_efs_access_point.artifacts.arn },
      Bool         = { "aws:SecureTransport" = "true", "elasticfilesystem:AccessedViaMountTarget" = "true" }
    }
  }] })
}
resource "aws_efs_file_system_policy" "artifacts" {
  file_system_id = aws_efs_file_system.artifacts.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Sid    = "AllowArtifactNodeIdentity", Effect = "Allow", Principal = { AWS = aws_iam_role.storage["artifacts"].arn },
      Action = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"], Resource = aws_efs_file_system.artifacts.arn,
      Condition = {
        StringEquals = { "elasticfilesystem:AccessPointArn" = aws_efs_access_point.artifacts.arn },
        Bool         = { "elasticfilesystem:AccessedViaMountTarget" = "true" }
      }
    },
    { Sid = "DenyCleartext", Effect = "Deny", Principal = { AWS = "*" }, Action = "elasticfilesystem:Client*", Resource = aws_efs_file_system.artifacts.arn, Condition = { Bool = { "aws:SecureTransport" = "false" } } },
    { Sid = "DenyOtherPaths", Effect = "Deny", Principal = { AWS = "*" }, Action = "elasticfilesystem:Client*", Resource = aws_efs_file_system.artifacts.arn, Condition = { StringNotEquals = { "elasticfilesystem:AccessPointArn" = aws_efs_access_point.artifacts.arn } } },
    { Sid = "DenyRoot", Effect = "Deny", Principal = { AWS = "*" }, Action = "elasticfilesystem:ClientRootAccess", Resource = aws_efs_file_system.artifacts.arn }
  ] })
}

output "storage_manifest" {
  description = "Retained resources, not a claim of mounted storage or verified backups."
  value = {
    schema_version = "klip-platform-storage-allocation.v1",
    account_id     = local.config.account_id, region = local.config.region, project = local.config.project, cluster_id = local.config.cluster_id,
    artifacts      = { file_system_id = aws_efs_file_system.artifacts.id, access_point_id = aws_efs_access_point.artifacts.id, file_system_arn = aws_efs_file_system.artifacts.arn, node_role_arn = aws_iam_role.storage["artifacts"].arn, uid = local.storage.artifacts.uid, gid = local.storage.artifacts.gid },
    database       = { bucket = aws_s3_bucket.database.id, archive_prefix = local.storage.database.archive_prefix, role_arn = aws_iam_role.storage["database"].arn, namespace = local.storage.database.namespace, service_accounts = local.storage.database.service_accounts }
  }
}
