output "dns_name" {
  description = "Stable-for-NLB-lifetime DNS name, used by future kubeadm configuration."
  value       = aws_lb.api.dns_name
}
output "arn" {
  description = "API NLB ARN."
  value       = aws_lb.api.arn
}
output "target_group_arn" {
  description = "Target group used for transport smoke tests and future API health checks."
  value       = aws_lb_target_group.api.arn
}
