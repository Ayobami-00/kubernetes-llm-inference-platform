locals {
  config     = jsondecode(file("${path.module}/../../project.json"))
  platform   = jsondecode(file("${path.module}/../../platform.json"))
  foundation = data.terraform_remote_state.foundation.outputs.foundation_manifest
  workers    = data.terraform_remote_state.platform.outputs.platform_manifest
  storage    = data.terraform_remote_state.storage.outputs.storage_manifest
  tags = {
    Project     = local.config.project, Cluster = local.config.cluster_id,
    Environment = local.config.environment, ManagedBy = "terraform-platform-storage-access"
  }
}

provider "aws" {
  region              = local.config.region
  allowed_account_ids = [local.config.account_id]
}

data "terraform_remote_state" "foundation" {
  backend = "s3"
  config = {
    bucket = local.config.state_bucket, key = "aws-foundation/terraform.tfstate",
    region = local.config.region, encrypt = true
  }
}
data "terraform_remote_state" "platform" {
  backend = "s3"
  config = {
    bucket = local.config.state_bucket, key = "platform-workers/terraform.tfstate",
    region = local.config.region, encrypt = true
  }
}
data "terraform_remote_state" "storage" {
  backend = "s3"
  config = {
    bucket = local.config.state_bucket, key = "platform-storage/terraform.tfstate",
    region = local.config.region, encrypt = true
  }
}

resource "terraform_data" "ownership_guard" {
  input = local.foundation.network.vpc_id
  lifecycle {
    precondition {
      condition     = alltrue([for source in [local.foundation, local.workers, local.storage] : source.account_id == local.config.account_id && source.region == local.config.region && source.project == local.config.project && source.cluster_id == local.config.cluster_id]) && local.workers.network.vpc_id == local.foundation.network.vpc_id
      error_message = "Storage connectivity may only join this project's verified allocation states."
    }
  }
}

resource "aws_security_group" "mount_targets" {
  name        = "klip-artifact-mounts"
  description = "TLS/IAM artifact mounts from persistent CPU workers only"
  vpc_id      = local.foundation.network.vpc_id
  tags        = local.tags
}
resource "aws_vpc_security_group_ingress_rule" "nfs" {
  security_group_id            = aws_security_group.mount_targets.id
  referenced_security_group_id = local.workers.network.security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 2049
  to_port                      = 2049
  tags                         = local.tags
}
resource "aws_vpc_security_group_egress_rule" "nfs" {
  security_group_id            = local.workers.network.security_group_id
  referenced_security_group_id = aws_security_group.mount_targets.id
  ip_protocol                  = "tcp"
  from_port                    = 2049
  to_port                      = 2049
  tags                         = local.tags
}
resource "aws_efs_mount_target" "artifacts" {
  for_each        = local.platform.nodes
  file_system_id  = local.storage.artifacts.file_system_id
  subnet_id       = local.foundation.network.cpu_subnet_ids[each.value.foundation_slot]
  security_groups = [aws_security_group.mount_targets.id]
  depends_on      = [terraform_data.ownership_guard, aws_vpc_security_group_ingress_rule.nfs, aws_vpc_security_group_egress_rule.nfs]
}

output "storage_access_manifest" {
  description = "Rebuildable VPC connectivity; retained EFS and archives live in a separate state."
  value = {
    schema_version = "klip-platform-storage-access.v1",
    account_id     = local.config.account_id, region = local.config.region, project = local.config.project, cluster_id = local.config.cluster_id,
    vpc_id         = local.foundation.network.vpc_id, security_group_id = aws_security_group.mount_targets.id,
    mount_targets  = { for name, target in aws_efs_mount_target.artifacts : name => { id = target.id, ip_address = target.ip_address, availability_zone = local.platform.nodes[name].availability_zone } }
  }
}
