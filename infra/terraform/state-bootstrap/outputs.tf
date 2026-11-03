output "state_bucket" {
  description = "Protected, versioned state bucket retained through foundation teardown."
  value       = aws_s3_bucket.state.id
}
output "foundation_role_arn" {
  description = "MFA-protected foundation deployment role."
  value       = aws_iam_role.foundation.arn
}
output "dns_role_arn" {
  description = "MFA-protected, zone-scoped DNS deployment role."
  value       = aws_iam_role.dns.arn
}
output "node_boundary_arn" {
  description = "Boundary required when creating the future control-plane instance role."
  value       = aws_iam_policy.access["node-boundary"].arn
}
