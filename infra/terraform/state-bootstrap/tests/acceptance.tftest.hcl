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

variables { adopt_existing_access = false }
run "protected_native_s3_bootstrap" {
  command = plan
  assert {
    condition     = !aws_s3_bucket.state.force_destroy && aws_s3_bucket_public_access_block.state.block_public_policy && aws_s3_bucket_public_access_block.state.restrict_public_buckets
    error_message = "State storage must remain protected from destruction and public access."
  }
  assert {
    condition     = aws_s3_bucket_versioning.state.versioning_configuration[0].status == "Enabled"
    error_message = "State recovery requires versioning."
  }
  assert {
    condition     = aws_iam_role.foundation.max_session_duration == 3600 && aws_iam_role.dns.max_session_duration == 3600
    error_message = "Deployment sessions must be bounded."
  }
}
