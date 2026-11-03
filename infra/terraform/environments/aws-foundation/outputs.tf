output "foundation_manifest" {
  description = "Versioned allocation manifest; live observations and a real CA are separate."
  value = {
    schema_version      = "foundation-allocation.v1"
    cluster_id          = local.config.cluster_id
    account_id          = local.config.account_id
    region              = local.config.region
    project             = local.config.project
    ami_id              = local.config.ami_id
    instance_type       = local.config.instance_type
    deletion_protection = var.deletion_protection
    network = {
      vpc_id                 = module.network.vpc_id
      vpc_cidr               = local.config.vpc_cidr
      wireguard_cidr         = local.config.wireguard_cidr
      pod_cidr               = local.config.pod_cidr
      service_cidr           = local.config.service_cidr
      worker_wireguard_range = local.config.worker_wireguard_range
      api_routed_cidrs       = [for node in values(local.config.nodes) : node.api_subnet_cidr]
      cpu_subnet_ids         = module.network.cpu_subnet_ids
      api_subnet_ids         = module.network.api_subnet_ids
      cpu_security_group_id  = module.network.cpu_security_group_id
      api_security_group_id  = module.network.api_security_group_id
    }
    api = {
      endpoint         = "https://${module.api.dns_name}:6443"
      dns_name         = module.api.dns_name
      arn              = module.api.arn
      target_group_arn = module.api.target_group_arn
      frontend_ips     = [for node in values(local.config.nodes) : node.api_ip]
    }
    inference_endpoint = "https://${local.config.inference_hostname}"
    nodes = { for key, node in module.compute.nodes : key => merge(node, {
      ssh_user = "ubuntu", ssh_credential_ref = local.config.ssh_credential_ref
    }) }
  }
}
