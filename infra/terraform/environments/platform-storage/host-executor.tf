# Host operations retain SSH/peer custody separately from user-downloadable run
# artifacts. No management/runner access point can traverse this filesystem.
resource "aws_efs_file_system" "host_executor" {
  creation_token   = "klip-retained-host-executor"
  encrypted        = true
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"
  tags             = merge(local.tags, { Name = "klip-host-executor" })
  lifecycle { prevent_destroy = true }
}
resource "aws_efs_access_point" "host_executor" {
  file_system_id = aws_efs_file_system.host_executor.id
  posix_user {
    uid = 20002
    gid = 20002
  }
  root_directory {
    path = "/executor"
    creation_info {
      owner_uid   = 20002
      owner_gid   = 20002
      permissions = "0700"
    }
  }
  tags = local.tags
  lifecycle { prevent_destroy = true }
}
resource "aws_efs_backup_policy" "host_executor" {
  file_system_id = aws_efs_file_system.host_executor.id
  backup_policy { status = "ENABLED" }
}
resource "aws_iam_role_policy" "host_executor_storage" {
  name = "host-executor-access-point-only"
  role = aws_iam_role.storage["artifacts"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow", Action = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"],
    Resource = aws_efs_file_system.host_executor.arn,
    Condition = {
      StringEquals = { "elasticfilesystem:AccessPointArn" = aws_efs_access_point.host_executor.arn },
      Bool         = { "aws:SecureTransport" = "true", "elasticfilesystem:AccessedViaMountTarget" = "true" }
    }
  }] })
}
resource "aws_efs_file_system_policy" "host_executor" {
  file_system_id = aws_efs_file_system.host_executor.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Sid    = "AllowHostStateNodeIdentity", Effect = "Allow", Principal = { AWS = aws_iam_role.storage["artifacts"].arn },
      Action = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"], Resource = aws_efs_file_system.host_executor.arn,
      Condition = {
        StringEquals = { "elasticfilesystem:AccessPointArn" = aws_efs_access_point.host_executor.arn },
        Bool         = { "elasticfilesystem:AccessedViaMountTarget" = "true" }
      }
    },
    { Sid = "DenyCleartext", Effect = "Deny", Principal = { AWS = "*" }, Action = "elasticfilesystem:Client*", Resource = aws_efs_file_system.host_executor.arn, Condition = { Bool = { "aws:SecureTransport" = "false" } } },
    { Sid = "DenyOtherPaths", Effect = "Deny", Principal = { AWS = "*" }, Action = "elasticfilesystem:Client*", Resource = aws_efs_file_system.host_executor.arn, Condition = { StringNotEquals = { "elasticfilesystem:AccessPointArn" = aws_efs_access_point.host_executor.arn } } },
    { Sid = "DenyRoot", Effect = "Deny", Principal = { AWS = "*" }, Action = "elasticfilesystem:ClientRootAccess", Resource = aws_efs_file_system.host_executor.arn }
  ] })
}
