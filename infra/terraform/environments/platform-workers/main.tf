locals {
  config   = jsondecode(file("${path.module}/../../project.json"))
  platform = jsondecode(file("${path.module}/../../platform.json"))
  tags = {
    Project     = local.config.project, Cluster = local.config.cluster_id,
    Environment = local.config.environment, ManagedBy = "terraform-platform-workers"
  }
  foundation = data.terraform_remote_state.foundation.outputs.foundation_manifest
}

data "terraform_remote_state" "foundation" {
  backend = "s3"
  config = {
    bucket  = local.config.state_bucket
    key     = "aws-foundation/terraform.tfstate"
    region  = local.config.region
    encrypt = true
  }
}

variable "ssh_public_key" {
  description = "Existing foundation public key; private material stays outside Terraform."
  type        = string
}
variable "deletion_protection" {
  description = "Disable only after verified drain and retained-state backup."
  type        = bool
  default     = true
}
variable "instance_type_overrides" {
  description = "Reviewed rolling-maintenance sizes. Empty rebuilds every worker at the versioned default; node outputs always report their actual planned sizes."
  type        = map(string)
  default     = {}
  validation {
    condition     = alltrue([for name, size in var.instance_type_overrides : contains(["platform-01", "platform-02", "platform-03"], name) && can(regex("^[a-z][a-z0-9]*\\.[a-z0-9]+$", size))])
    error_message = "Maintenance overrides may name only the three persistent platform slots and explicit instance types."
  }
}

resource "terraform_data" "foundation_identity" {
  input = local.foundation.cluster_id
  lifecycle {
    precondition {
      condition     = local.foundation.cluster_id == local.config.cluster_id && local.foundation.account_id == local.config.account_id && local.foundation.region == local.config.region && local.foundation.project == local.config.project
      error_message = "Platform workers must use this project's existing foundation identity."
    }
    precondition {
      condition     = alltrue([for node in values(local.platform.nodes) : contains(keys(local.foundation.nodes), node.foundation_slot)])
      error_message = "Platform slots must reference actual foundation subnets."
    }
  }
}

module "compute" {
  source                   = "../../modules/platform-compute"
  vpc_id                   = local.foundation.network.vpc_id
  ami_id                   = local.config.ami_id
  instance_type            = local.platform.instance_type
  root_volume_gib          = local.platform.root_volume_gib
  permissions_boundary_arn = "arn:aws:iam::${local.config.account_id}:policy/${local.config.project}/kubernetes-llm-control-plane-boundary"
  ssh_public_key           = var.ssh_public_key
  deletion_protection      = var.deletion_protection
  tags                     = local.tags
  nodes = { for name, node in local.platform.nodes : name => {
    availability_zone = node.availability_zone
    subnet_id         = local.foundation.network.cpu_subnet_ids[node.foundation_slot]
    private_ip        = node.private_ip
    wireguard_ip      = node.wireguard_ip
    instance_type     = lookup(var.instance_type_overrides, name, local.platform.instance_type)
  } }
  depends_on = [terraform_data.foundation_identity]
}
