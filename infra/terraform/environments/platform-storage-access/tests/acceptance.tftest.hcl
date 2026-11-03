mock_provider "aws" {
  override_during = plan
  mock_resource "aws_security_group" { defaults = { id = "sg-00000000000000001" } }
  mock_resource "aws_efs_mount_target" { defaults = { id = "fsmt-00000000000000001", ip_address = "10.40.0.100" } }
}
override_data {
  target = data.terraform_remote_state.foundation
  values = { outputs = { foundation_manifest = {
    account_id = "127811062476", region = "eu-west-2", project = "kubernetes-llm-inference-platform", cluster_id = "klip",
    network    = { vpc_id = "vpc-00000000000000001", cpu_subnet_ids = { cp-01 = "subnet-00000000000000001", cp-02 = "subnet-00000000000000002", cp-03 = "subnet-00000000000000003" } }
  } } }
}
override_data {
  target = data.terraform_remote_state.platform
  values = { outputs = { platform_manifest = {
    account_id = "127811062476", region = "eu-west-2", project = "kubernetes-llm-inference-platform", cluster_id = "klip",
    network    = { vpc_id = "vpc-00000000000000001", security_group_id = "sg-00000000000000002" }
  } } }
}
override_data {
  target = data.terraform_remote_state.storage
  values = { outputs = { storage_manifest = {
    account_id = "127811062476", region = "eu-west-2", project = "kubernetes-llm-inference-platform", cluster_id = "klip",
    artifacts  = { file_system_id = "fs-00000000000000001" }
  } } }
}
run "only_scoped_cpu_nfs_connectivity" {
  command = plan
  assert {
    condition     = length(aws_efs_mount_target.artifacts) == 3 && aws_efs_mount_target.artifacts["platform-02"].subnet_id == "subnet-00000000000000002"
    error_message = "Use one local mount target in each platform availability zone."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.nfs.from_port == 2049 && aws_vpc_security_group_ingress_rule.nfs.to_port == 2049 && aws_vpc_security_group_ingress_rule.nfs.referenced_security_group_id == "sg-00000000000000002" && aws_vpc_security_group_ingress_rule.nfs.cidr_ipv4 == null && aws_vpc_security_group_ingress_rule.nfs.cidr_ipv6 == null
    error_message = "Only the persistent CPU security group may access the NFS port."
  }
  assert {
    condition     = aws_vpc_security_group_egress_rule.nfs.security_group_id == "sg-00000000000000002" && aws_vpc_security_group_egress_rule.nfs.referenced_security_group_id == aws_security_group.mount_targets.id && aws_vpc_security_group_egress_rule.nfs.from_port == 2049
    error_message = "Only scoped NFS egress may be added to CPU workers."
  }
}
run "reject_foreign_storage" {
  command = plan
  override_data {
    target = data.terraform_remote_state.storage
    values = { outputs = { storage_manifest = {
      account_id = "999999999999", region = "eu-west-2", project = "other", cluster_id = "other",
      artifacts  = { file_system_id = "fs-00000000000000001" }
    } } }
  }
  expect_failures = [terraform_data.ownership_guard]
}
