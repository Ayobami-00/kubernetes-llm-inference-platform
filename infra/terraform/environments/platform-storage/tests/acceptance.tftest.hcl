mock_provider "aws" {
  override_during = plan
  mock_resource "aws_s3_bucket" { defaults = { id = "klip-database-127811062476-eu-west-2", arn = "arn:aws:s3:::klip-database-127811062476-eu-west-2" } }
  mock_resource "aws_iam_role" { defaults = { id = "klip-storage-fixture", arn = "arn:aws:iam::127811062476:role/kubernetes-llm-inference-platform/klip-storage-fixture" } }
  mock_resource "aws_efs_file_system" { defaults = { id = "fs-00000000000000001", availability_zone_name = "", arn = "arn:aws:elasticfilesystem:eu-west-2:127811062476:file-system/fs-00000000000000001" } }
  mock_resource "aws_efs_access_point" { defaults = { id = "fsap-00000000000000001", arn = "arn:aws:elasticfilesystem:eu-west-2:127811062476:access-point/fsap-00000000000000001" } }
}
override_data {
  target = data.terraform_remote_state.identity
  values = { outputs = { identity_manifest = {
    account_id = "127811062476", region = "eu-west-2", project = "kubernetes-llm-inference-platform", cluster_id = "klip",
    issuer     = "https://oidc.klip-lab.click", audience = "sts.amazonaws.com", provider_arn = "arn:aws:iam::127811062476:oidc-provider/oidc.klip-lab.click"
  } } }
}

run "retained_encrypted_scoped_storage" {
  command = plan
  assert {
    condition     = aws_efs_file_system.artifacts.encrypted && aws_efs_file_system.artifacts.availability_zone_name == "" && aws_efs_file_system.artifacts.throughput_mode == "elastic" && aws_efs_backup_policy.artifacts.backup_policy[0].status == "ENABLED"
    error_message = "Artifact storage must be encrypted, regional and backed up."
  }
  assert {
    condition     = aws_efs_access_point.artifacts.posix_user[0].uid == 10001 && aws_efs_access_point.artifacts.posix_user[0].gid == 10001 && aws_efs_access_point.artifacts.root_directory[0].path == "/artifacts"
    error_message = "Enforce the existing non-root artifact identity on the access point."
  }
  assert {
    condition     = aws_s3_bucket_versioning.database.versioning_configuration[0].status == "Enabled" && aws_s3_bucket_public_access_block.database.block_public_policy && aws_s3_bucket_server_side_encryption_configuration.database.rule != null
    error_message = "Database archives require private, versioned, encrypted storage."
  }
  assert {
    condition     = alltrue([for rule in aws_s3_bucket_lifecycle_configuration.database.rule : length(rule.expiration) == 0 && rule.noncurrent_version_expiration[0].noncurrent_days == 45])
    error_message = "Do not independently expire current WAL/backups; retain deleted versions beyond the recovery window."
  }
  assert {
    condition     = toset(jsondecode(aws_iam_role.storage["database"].assume_role_policy).Statement[0].Condition.StringEquals["oidc.klip-lab.click:sub"]) == toset(["system:serviceaccount:platform-data:klip-db", "system:serviceaccount:platform-data:klip-db-restore"]) && jsondecode(aws_iam_role.storage["database"].assume_role_policy).Statement[0].Condition.StringEquals["oidc.klip-lab.click:aud"] == "sts.amazonaws.com"
    error_message = "Backup identity must bind exact isolated service accounts and STS audience."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.database.policy).Statement[2].Resource == "${aws_s3_bucket.database.arn}/postgresql/*" && jsondecode(aws_iam_role_policy.database.policy).Statement[1].Condition.StringLike["s3:prefix"][0] == "postgresql/*"
    error_message = "Barman object access and multipart operations stay within its archive prefix."
  }
  assert {
    condition     = toset(jsondecode(aws_iam_role_policy.database.policy).Statement[0].Action) == toset(["s3:GetBucketLocation", "s3:ListBucket"]) && jsondecode(aws_iam_role_policy.database.policy).Statement[0].Resource == aws_s3_bucket.database.arn
    error_message = "Barman HeadBucket requires ListBucket on the dedicated archive bucket, never other buckets."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.artifacts.policy).Statement[0].Condition.StringEquals["elasticfilesystem:AccessPointArn"] == aws_efs_access_point.artifacts.arn && toset(jsondecode(aws_iam_role_policy.artifacts.policy).Statement[0].Action) == toset(["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"])
    error_message = "The node driver gets only non-root access-point mount/write permissions."
  }
  assert {
    condition     = length([for s in jsondecode(aws_efs_file_system_policy.artifacts.policy).Statement : s if s.Effect == "Deny"]) == 3
    error_message = "EFS must deny cleartext, other paths and root access."
  }
  assert {
    condition     = aws_efs_access_point.host_executor.posix_user[0].uid == 20002 && aws_efs_access_point.host_executor.root_directory[0].path == "/executor" && aws_efs_access_point.host_executor.root_directory[0].creation_info[0].permissions == "0700" && aws_efs_file_system.host_executor.encrypted && aws_efs_backup_policy.host_executor.backup_policy[0].status == "ENABLED"
    error_message = "Host custody needs a separate encrypted backed-up non-root filesystem."
  }
  assert {
    condition     = length([for s in jsondecode(aws_efs_file_system_policy.host_executor.policy).Statement : s if s.Effect == "Deny"]) == 3 && jsondecode(aws_iam_role_policy.host_executor_storage.policy).Statement[0].Condition.StringEquals["elasticfilesystem:AccessPointArn"] == aws_efs_access_point.host_executor.arn
    error_message = "Host mounts must deny cleartext, other paths and root without widening artifact access."
  }
}

run "reject_unrelated_oidc" {
  command = plan
  override_data {
    target = data.terraform_remote_state.identity
    values = { outputs = { identity_manifest = {
      account_id = "999999999999", region = "eu-west-2", project = "other", cluster_id = "other",
      issuer     = "https://foreign.example", audience = "sts.amazonaws.com", provider_arn = "arn:aws:iam::999999999999:oidc-provider/foreign.example"
    } } }
  }
  expect_failures = [terraform_data.identity_guard]
}
