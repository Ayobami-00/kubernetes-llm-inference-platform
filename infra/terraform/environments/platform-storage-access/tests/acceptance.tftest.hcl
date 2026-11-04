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
    nodes      = { cp-01 = { instance_id = "i-00000000000000011" }, cp-02 = { instance_id = "i-00000000000000012" }, cp-03 = { instance_id = "i-00000000000000013" } }
  } } }
}
override_data {
  target = data.terraform_remote_state.platform
  values = { outputs = { platform_manifest = {
    account_id = "127811062476", region = "eu-west-2", project = "kubernetes-llm-inference-platform", cluster_id = "klip",
    network    = { vpc_id = "vpc-00000000000000001", security_group_id = "sg-00000000000000002" }
    nodes      = { platform-01 = { instance_id = "i-00000000000000001" }, platform-02 = { instance_id = "i-00000000000000002" }, platform-03 = { instance_id = "i-00000000000000003" } }
  } } }
}
override_data {
  target = data.terraform_remote_state.storage
  values = { outputs = { storage_manifest = {
    account_id    = "127811062476", region = "eu-west-2", project = "kubernetes-llm-inference-platform", cluster_id = "klip",
    artifacts     = { file_system_id = "fs-00000000000000001" }
    host_executor = { file_system_id = "fs-00000000000000002" }
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
      account_id    = "999999999999", region = "eu-west-2", project = "other", cluster_id = "other",
      artifacts     = { file_system_id = "fs-00000000000000001" }
      host_executor = { file_system_id = "fs-00000000000000002" }
    } } }
  }
  expect_failures = [terraform_data.ownership_guard]
}

run "host_authority_is_separate_and_exact" {
  command = plan
  assert {
    condition     = jsondecode(aws_iam_role.host_executor.assume_role_policy).Statement[0].Condition.StringEquals["oidc.klip-lab.click:sub"] == "system:serviceaccount:platform-ops:klip-host-job"
    error_message = "Only the isolated execution account may assume SSH transport authority."
  }
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.host_executor.policy).Statement[0].Resource) == 7 && contains(jsondecode(aws_iam_role_policy.host_executor.policy).Statement[0].Resource, "arn:aws:ssm:eu-west-2::document/AWS-StartSSHSession") && jsondecode(aws_iam_role_policy.host_executor.policy).Statement[0].Action == ["ssm:StartSession"]
    error_message = "Only the six CPU hosts and SSH session document may be targeted."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.host_executor.policy).Statement[1].Condition.StringEquals["ssm:resourceTag/aws:ssmmessages:session-id"] == "$${aws:userid}" && length(aws_efs_mount_target.host_executor) == 3 && aws_efs_mount_target.host_executor["platform-01"].file_system_id != aws_efs_mount_target.artifacts["platform-01"].file_system_id
    error_message = "Session cleanup stays with its federated owner and host state has separate mounts."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.host_executor.policy).Statement[2].Action == ["ssmmessages:OpenDataChannel"] && jsondecode(aws_iam_role_policy.host_executor.policy).Statement[2].Resource == "arn:aws:ssm:eu-west-2:127811062476:session/klip-host-executor-*"
    error_message = "Websocket transport may open only the fixed executor session prefix."
  }
}

run "ebs_identity_is_bounded_to_the_platform" {
  command = plan
  assert {
    condition     = jsondecode(aws_iam_role.ebs.assume_role_policy).Statement[0].Condition.StringEquals["oidc.klip-lab.click:sub"] == "system:serviceaccount:kube-system:ebs-csi-controller-sa"
    error_message = "EBS credentials belong only to the exact controller account."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.ebs.policy).Statement[1].Condition.Bool["ec2:Encrypted"] == "true" && jsondecode(aws_iam_role_policy.ebs.policy).Statement[1].Condition.StringEquals["ec2:VolumeType"] == "gp3" && jsondecode(aws_iam_role_policy.ebs.policy).Statement[1].Condition.NumericLessThanEquals["ec2:VolumeSize"] == "200"
    error_message = "Initial CSI creation must be encrypted gp3 and size bounded."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.ebs.policy).Statement[3].Condition.StringEquals["ec2:ResourceTag/ebs.csi.aws.com/cluster"] == "true" && jsondecode(aws_iam_role_policy.ebs.policy).Statement[3].Condition.StringEquals["ec2:ResourceTag/klip.dev/cluster"] == "klip"
    error_message = "CSI cannot operate on untagged or foreign/control-plane disks."
  }
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.ebs.policy).Statement[4].Resource) == 3 && alltrue([for arn in jsondecode(aws_iam_role_policy.ebs.policy).Statement[4].Resource : startswith(arn, "arn:aws:ec2:eu-west-2:127811062476:instance/i-0000000000000000")]) && jsondecode(aws_iam_role_policy.ebs.policy).Statement[2].Condition.StringEquals["ec2:CreateAction"] == "CreateVolume"
    error_message = "Only current platform instances and creation-time tagging are permitted."
  }
}
