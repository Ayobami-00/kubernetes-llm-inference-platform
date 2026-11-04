resource "aws_efs_mount_target" "host_executor" {
  for_each        = local.platform.nodes
  file_system_id  = local.storage.host_executor.file_system_id
  subnet_id       = local.foundation.network.cpu_subnet_ids[each.value.foundation_slot]
  security_groups = [aws_security_group.mount_targets.id]
  depends_on      = [terraform_data.ownership_guard, aws_vpc_security_group_ingress_rule.nfs, aws_vpc_security_group_egress_rule.nfs]
}

# The API/controller has only namespaced Job RBAC. Only execution Jobs receive
# this workload identity, limited to SSH sessions against the six protected CPUs.
resource "aws_iam_role" "host_executor" {
  name                 = "klip-host-executor"
  path                 = "/${local.config.project}/"
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = "arn:aws:iam::${local.config.account_id}:oidc-provider/oidc.${local.config.domain}" },
    Condition = { StringEquals = {
      "oidc.${local.config.domain}:sub" = "system:serviceaccount:platform-ops:klip-host-job",
      "oidc.${local.config.domain}:aud" = "sts.amazonaws.com"
    } }
  }] })
  tags       = local.tags
  depends_on = [terraform_data.ownership_guard]
}
resource "aws_iam_role_policy" "host_executor" {
  name = "ssh-project-cpus-only"
  role = aws_iam_role.host_executor.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Effect = "Allow", Action = ["ssm:StartSession"],
      Resource = concat(
        [for node in concat(values(local.foundation.nodes), values(local.workers.nodes)) : "arn:aws:ec2:${local.config.region}:${local.config.account_id}:instance/${node.instance_id}"],
        ["arn:aws:ssm:${local.config.region}::document/AWS-StartSSHSession"]
      ),
      Condition = { StringEquals = { "aws:RequestedRegion" = local.config.region } }
    },
    {
      Effect   = "Allow", Action = ["ssm:TerminateSession"],
      Resource = "arn:aws:ssm:${local.config.region}:${local.config.account_id}:session/*",
      Condition = { StringEquals = {
        "ssm:resourceTag/aws:ssmmessages:session-id" = "$${aws:userid}"
      } }
    },
    {
      Effect   = "Allow", Action = ["ssmmessages:OpenDataChannel"],
      Resource = "arn:aws:ssm:${local.config.region}:${local.config.account_id}:session/klip-host-executor-*"
    }
  ] })
}
