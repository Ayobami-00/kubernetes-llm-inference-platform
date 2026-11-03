locals {
  config = jsondecode(file("${path.module}/../../project.json"))
  tags = {
    Project     = local.config.project, Cluster = local.config.cluster_id,
    Environment = local.config.environment, ManagedBy = "terraform-inference-dns"
  }
}

variable "origins" {
  description = "Foundation-reviewed active Lambda gateway origins; empty until gateways exist."
  type        = map(object({ ipv4 = string, weight = optional(number, 1) }))
  default     = {}
}
variable "health_path" {
  description = "HTTPS gateway readiness path, configured when the gateway integration is pinned."
  type        = string
  default     = "/readyz"
}

module "inference_dns" {
  source         = "../../modules/inference-dns"
  hosted_zone_id = local.config.hosted_zone_id
  hostname       = local.config.inference_hostname
  origins        = var.origins
  health_path    = var.health_path
  tags           = local.tags
}

output "inference_endpoint" {
  description = "Intended endpoint, not a readiness assertion."
  value       = module.inference_dns.endpoint
}
output "origins" {
  description = "Configured gateway records; empty when no real origins are supplied."
  value       = module.inference_dns.origins
}
