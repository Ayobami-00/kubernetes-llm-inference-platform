output "vpc_id" {
  description = "Dedicated foundation VPC."
  value       = aws_vpc.this.id
}
output "cpu_subnet_ids" {
  description = "CPU subnet IDs by stable node slot."
  value       = { for key, subnet in aws_subnet.cpu : key => subnet.id }
}
output "api_subnet_ids" {
  description = "Private NLB subnet IDs by stable node slot."
  value       = { for key, subnet in aws_subnet.api : key => subnet.id }
}
output "cpu_security_group_id" {
  description = "Control-plane host security group."
  value       = aws_security_group.cpu.id
}
output "api_security_group_id" {
  description = "NLB frontend/backend security group."
  value       = aws_security_group.api.id
}
output "internet_route_id" {
  description = "Dependency ensuring host egress is configured before boot."
  value       = aws_route.internet.id
}
