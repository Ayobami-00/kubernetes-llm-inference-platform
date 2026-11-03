locals {
  config = jsondecode(file("${path.module}/../../project.json"))
  tags = {
    Project     = local.config.project, Cluster = local.config.cluster_id,
    Environment = local.config.environment, ManagedBy = "terraform-aws-foundation"
  }
}

data "aws_ami" "pinned" {
  owners = [local.config.ami_owner]
  filter {
    name   = "image-id"
    values = [local.config.ami_id]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
  filter {
    name   = "state"
    values = ["available"]
  }
}

data "aws_availability_zones" "selected" {
  state = "available"
  filter {
    name   = "zone-name"
    values = [for node in values(local.config.nodes) : node.availability_zone]
  }
  lifecycle {
    postcondition {
      condition     = length(self.names) == 3
      error_message = "All three explicitly selected London AZs must be available."
    }
  }
}

module "network" {
  source         = "../../modules/network"
  vpc_cidr       = local.config.vpc_cidr
  overlay_cidrs  = [local.config.wireguard_cidr, local.config.pod_cidr, local.config.service_cidr]
  reserved_cidrs = var.reserved_cidrs
  nodes          = local.config.nodes
  tags           = local.tags
  depends_on     = [data.aws_availability_zones.selected]
}

module "compute" {
  source                   = "../../modules/control-plane-compute"
  nodes                    = local.config.nodes
  subnet_ids               = module.network.cpu_subnet_ids
  security_group_id        = module.network.cpu_security_group_id
  ami_id                   = data.aws_ami.pinned.id
  instance_type            = local.config.instance_type
  permissions_boundary_arn = "arn:aws:iam::${local.config.account_id}:policy/${local.config.project}/kubernetes-llm-control-plane-boundary"
  ssh_public_key           = var.ssh_public_key
  deletion_protection      = var.deletion_protection
  tags                     = local.tags
  depends_on               = [module.network]
}

module "api" {
  source              = "../../modules/api-endpoint"
  vpc_id              = module.network.vpc_id
  security_group_id   = module.network.api_security_group_id
  frontends           = { for key, node in local.config.nodes : key => { subnet_id = module.network.api_subnet_ids[key], private_ip = node.api_ip } }
  backend_ips         = { for key, node in module.compute.nodes : key => node.private_ip }
  deletion_protection = var.deletion_protection
  tags                = local.tags
}
