mock_provider "aws" {
  mock_data "aws_ami" { defaults = { id = "ami-057b46c043e81dcf4" } }
  mock_data "aws_availability_zones" { defaults = { names = ["eu-west-2a", "eu-west-2b", "eu-west-2c"] } }
  mock_data "aws_route53_zone" { defaults = { name = "klip-lab.click.", private_zone = false, zone_id = "Z06158312NEW6IHHPH5XV" } }
  mock_resource "aws_vpc" { defaults = { id = "vpc-00000000000000001" } }
  mock_resource "aws_subnet" { defaults = { id = "subnet-00000000000000001" } }
  mock_resource "aws_security_group" { defaults = { id = "sg-00000000000000001" } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::127811062476:role/kubernetes-llm-inference-platform/klip-control-plane" } }
  mock_resource "aws_iam_policy" { defaults = { arn = "arn:aws:iam::127811062476:policy/kubernetes-llm-inference-platform/mock" } }
  mock_resource "aws_lb" { defaults = { arn = "arn:aws:elasticloadbalancing:eu-west-2:127811062476:loadbalancer/net/klip-api/0000000000000001", dns_name = "internal-klip-api.example.elb.amazonaws.com" } }
  mock_resource "aws_lb_target_group" { defaults = { arn = "arn:aws:elasticloadbalancing:eu-west-2:127811062476:targetgroup/klip-api/0000000000000001" } }
  mock_resource "aws_eip" { defaults = { public_ip = "203.0.113.10" } }
}

variables {
  hosted_zone_id = "Z06158312NEW6IHHPH5XV"
  hostname       = "inference.klip-lab.click"
  tags           = { Project = "kubernetes-llm-inference-platform" }
}
run "empty_means_no_origins" {
  command = plan
  assert {
    condition     = length(aws_route53_record.gateway) == 0 && length(aws_route53_health_check.gateway) == 0
    error_message = "Do not create records or health checks without real origins."
  }
}
run "weighted_https_origins" {
  command = plan
  # Mock-only public IPs; these tests never make AWS or gateway calls.
  variables { origins = { gpu-one = { ipv4 = "8.8.8.8" } } }
  assert {
    condition     = aws_route53_record.gateway["gpu-one"].ttl == 30 && aws_route53_record.gateway["gpu-one"].weighted_routing_policy[0].weight == 1 && aws_route53_health_check.gateway["gpu-one"].enable_sni
    error_message = "Use health-checked weighted records and HTTPS SNI."
  }
}
run "reject_private_origins" {
  command = plan
  variables { origins = { gpu-one = { ipv4 = "10.0.0.5" } } }
  expect_failures = [data.aws_route53_zone.existing]
}
run "reject_zero_weight" {
  command = plan
  variables { origins = { gpu-one = { ipv4 = "8.8.8.8", weight = 0 } } }
  expect_failures = [var.origins]
}
