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
  vpc_cidr      = "10.40.0.0/16"
  overlay_cidrs = ["10.200.0.0/24", "10.244.0.0/16", "10.96.0.0/12"]
  nodes         = { "cp-01" : { "availability_zone" : "eu-west-2a", "public_subnet_cidr" : "10.40.0.0/24", "private_ip" : "10.40.0.10", "api_subnet_cidr" : "10.40.16.0/24", "api_ip" : "10.40.16.10", "wireguard_ip" : "10.200.0.11" }, "cp-02" : { "availability_zone" : "eu-west-2b", "public_subnet_cidr" : "10.40.1.0/24", "private_ip" : "10.40.1.10", "api_subnet_cidr" : "10.40.17.0/24", "api_ip" : "10.40.17.10", "wireguard_ip" : "10.200.0.12" }, "cp-03" : { "availability_zone" : "eu-west-2c", "public_subnet_cidr" : "10.40.2.0/24", "private_ip" : "10.40.2.10", "api_subnet_cidr" : "10.40.18.0/24", "api_ip" : "10.40.18.10", "wireguard_ip" : "10.200.0.13" } }
  tags          = { "Project" : "kubernetes-llm-inference-platform" }
}
run "three_zone_network" {
  command = plan
  assert {
    condition     = length(aws_subnet.cpu) == 3 && length(aws_subnet.api) == 3
    error_message = "Exactly three CPU and private API subnets are required."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.wireguard.ip_protocol == "udp" && aws_vpc_security_group_ingress_rule.wireguard.from_port == 51820
    error_message = "Only the WireGuard UDP transport is public."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.api_client.cidr_ipv4 == null && aws_vpc_security_group_ingress_rule.api_backend.cidr_ipv4 == null
    error_message = "API access must use SG references, not public CIDRs."
  }
}
run "reject_overlapping_overlays" {
  command = plan
  variables { overlay_cidrs = ["10.40.0.0/24", "10.244.0.0/16", "10.96.0.0/12"] }
  expect_failures = [aws_vpc.this]
}
run "reject_missing_owner" {
  command = plan
  variables { tags = {} }
  expect_failures = [var.tags]
}
