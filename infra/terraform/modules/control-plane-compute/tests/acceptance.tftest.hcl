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
  nodes                    = { "cp-01" : { "availability_zone" : "eu-west-2a", "public_subnet_cidr" : "10.40.0.0/24", "private_ip" : "10.40.0.10", "api_subnet_cidr" : "10.40.16.0/24", "api_ip" : "10.40.16.10", "wireguard_ip" : "10.200.0.11" }, "cp-02" : { "availability_zone" : "eu-west-2b", "public_subnet_cidr" : "10.40.1.0/24", "private_ip" : "10.40.1.10", "api_subnet_cidr" : "10.40.17.0/24", "api_ip" : "10.40.17.10", "wireguard_ip" : "10.200.0.12" }, "cp-03" : { "availability_zone" : "eu-west-2c", "public_subnet_cidr" : "10.40.2.0/24", "private_ip" : "10.40.2.10", "api_subnet_cidr" : "10.40.18.0/24", "api_ip" : "10.40.18.10", "wireguard_ip" : "10.200.0.13" } }
  subnet_ids               = { "cp-01" : "subnet-00000000000000001", "cp-02" : "subnet-00000000000000002", "cp-03" : "subnet-00000000000000003" }
  security_group_id        = "sg-00000000000000001"
  ami_id                   = "ami-057b46c043e81dcf4"
  permissions_boundary_arn = "arn:aws:iam::127811062476:policy/kubernetes-llm-inference-platform/kubernetes-llm-control-plane-boundary"
  ssh_public_key           = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIG8K44GFhFQd1dWoZLXK9Q6Zv3hNqqEPJfcQ4LfZxZxm offline-test"
  tags                     = { "Project" : "kubernetes-llm-inference-platform" }
}
run "protected_encrypted_hosts" {
  command = plan
  assert {
    condition     = alltrue([for host in values(aws_instance.node) : host.disable_api_termination && host.metadata_options[0].http_tokens == "required" && host.metadata_options[0].http_put_response_hop_limit == 1 && host.root_block_device[0].encrypted && host.root_block_device[0].volume_size == 40])
    error_message = "Hosts must be protected, encrypted and require IMDSv2 with hop limit one."
  }
  assert {
    condition     = alltrue([for disk in values(aws_ebs_volume.etcd) : disk.encrypted && disk.size == 20 && disk.type == "gp3"])
    error_message = "Dedicated etcd disks must be encrypted 20 GiB gp3 volumes."
  }
  assert {
    condition     = alltrue([for interface in values(aws_network_interface.node) : !interface.source_dest_check])
    error_message = "Primary ENIs must accommodate later mesh forwarding."
  }
}
