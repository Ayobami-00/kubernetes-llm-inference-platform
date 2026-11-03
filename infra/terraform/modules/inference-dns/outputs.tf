output "endpoint" {
  description = "Intended serving endpoint; an empty origin map does not make it reachable."
  value       = "https://${var.hostname}"
}
output "origins" {
  description = "Configured record identities and health checks."
  value = { for key, origin in var.origins : key => {
    ipv4 = origin.ipv4, weight = origin.weight, health_check_id = aws_route53_health_check.gateway[key].id
  } }
}
