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
  ssh_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIG8K44GFhFQd1dWoZLXK9Q6Zv3hNqqEPJfcQ4LfZxZxm offline-test"
}
run "foundation_is_private_and_protected" {
  command = plan
  assert {
    condition     = output.foundation_manifest.schema_version == "foundation-allocation.v1" && length(output.foundation_manifest.nodes) == 3
    error_message = "Allocation output must include the three stable CPU slots."
  }
  assert {
    condition     = output.foundation_manifest.deletion_protection
    error_message = "Normal configuration must preserve deletion protection."
  }
}
