locals {
  ebs_volume_arn = "arn:aws:ec2:${local.config.region}:${local.config.account_id}:volume/*"
  ebs_owned = { StringEquals = {
    "ec2:ResourceTag/ebs.csi.aws.com/cluster" = "true"
    "ec2:ResourceTag/klip.dev/cluster"        = local.config.cluster_id
  } }
}

# Rebuildable controller identity belongs with this cluster's access layer, not
# with the retained filesystem/archive. No permissions are added to host roles.
resource "aws_iam_role" "ebs" {
  name                 = "klip-ebs-controller"
  path                 = "/${local.config.project}/"
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = "arn:aws:iam::${local.config.account_id}:oidc-provider/oidc.${local.config.domain}" },
    Condition = { StringEquals = {
      "oidc.${local.config.domain}:sub" = "system:serviceaccount:kube-system:ebs-csi-controller-sa"
      "oidc.${local.config.domain}:aud" = "sts.amazonaws.com"
    } }
  }] })
  tags       = local.tags
  depends_on = [terraform_data.ownership_guard]
}
resource "aws_iam_role_policy" "ebs" {
  name = "project-csi-volumes-platform-instances"
  role = aws_iam_role.ebs.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Sid       = "RegionalDiscovery", Effect = "Allow", Resource = "*",
      Action    = ["ec2:DescribeAvailabilityZones", "ec2:DescribeInstances", "ec2:DescribeInstanceTypes", "ec2:DescribeTags", "ec2:DescribeVolumes", "ec2:DescribeVolumesModifications", "ec2:DescribeVolumeStatus"],
      Condition = { StringEquals = { "aws:RequestedRegion" = local.config.region } }
    },
    {
      Sid = "BoundedEncryptedCreation", Effect = "Allow", Action = ["ec2:CreateVolume"], Resource = local.ebs_volume_arn,
      Condition = {
        StringEquals = {
          "aws:RequestTag/ebs.csi.aws.com/cluster" = "true", "aws:RequestTag/klip.dev/cluster" = local.config.cluster_id,
          "ec2:VolumeType"                         = "gp3"
        },
        Bool = { "ec2:Encrypted" = "true" }, NumericLessThanEquals = { "ec2:VolumeSize" = "200" }
      }
    },
    {
      Sid       = "TagOnlyAtCreation", Effect = "Allow", Action = ["ec2:CreateTags"], Resource = local.ebs_volume_arn,
      Condition = { StringEquals = { "ec2:CreateAction" = "CreateVolume" } }
    },
    {
      Sid      = "OperateOnlyOwnedVolumes", Effect = "Allow",
      Action   = ["ec2:DeleteVolume", "ec2:ModifyVolume", "ec2:AttachVolume", "ec2:DetachVolume"],
      Resource = local.ebs_volume_arn, Condition = local.ebs_owned
    },
    {
      Sid      = "AttachOnlyPlatformInstances", Effect = "Allow", Action = ["ec2:AttachVolume", "ec2:DetachVolume"],
      Resource = [for node in values(local.workers.nodes) : "arn:aws:ec2:${local.config.region}:${local.config.account_id}:instance/${node.instance_id}"]
    }
  ] })
}
