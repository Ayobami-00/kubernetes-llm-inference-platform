output "platform_manifest" {
  description = "Persistent AWS worker allocation, to be prepared and joined through B's interface."
  value = {
    schema_version = "klip-platform-allocation.v1"
    cluster_id     = local.config.cluster_id
    account_id     = local.config.account_id
    region         = local.config.region
    project        = local.config.project
    instance_type  = local.platform.instance_type
    network = {
      vpc_id            = local.foundation.network.vpc_id
      security_group_id = module.compute.security_group_id
    }
    nodes = module.compute.nodes
  }
}
