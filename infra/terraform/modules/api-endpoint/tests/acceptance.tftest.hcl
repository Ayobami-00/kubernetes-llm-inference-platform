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
  vpc_id            = "vpc-00000000000000001"
  security_group_id = "sg-00000000000000001"
  frontends = {
    cp-01 = { subnet_id = "subnet-00000000000000001", private_ip = "10.40.16.10" }
    cp-02 = { subnet_id = "subnet-00000000000000002", private_ip = "10.40.17.10" }
    cp-03 = { subnet_id = "subnet-00000000000000003", private_ip = "10.40.18.10" }
  }
  backend_ips = { cp-01 = "10.40.0.10", cp-02 = "10.40.1.10", cp-03 = "10.40.2.10" }
  tags        = { Project = "kubernetes-llm-inference-platform" }
}
run "private_tcp_endpoint" {
  command = plan
  assert {
    condition     = aws_lb.api.internal && aws_lb.api.enable_cross_zone_load_balancing && aws_lb.api.enable_deletion_protection
    error_message = "The API NLB must remain private, cross-zone and protected."
  }
  assert {
    condition     = aws_lb_target_group.api.target_type == "ip" && !aws_lb_target_group.api.preserve_client_ip && aws_lb_target_group.api.health_check[0].interval == 10
    error_message = "Use private IP targets without client-IP preservation and explicit health intervals."
  }
  assert {
    condition     = aws_lb_listener.api.port == 6443 && aws_lb_listener.api.protocol == "TCP"
    error_message = "The API uses TCP passthrough on 6443."
  }
}
