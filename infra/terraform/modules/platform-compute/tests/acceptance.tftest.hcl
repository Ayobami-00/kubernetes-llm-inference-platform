mock_provider "aws" {
  mock_resource "aws_security_group" { defaults = { id = "sg-00000000000000001" } }
  mock_resource "aws_network_interface" { defaults = { id = "eni-00000000000000001" } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::127811062476:role/kubernetes-llm-inference-platform/klip-platform-worker" } }
  mock_resource "aws_instance" { defaults = { id = "i-00000000000000001" } }
  mock_resource "aws_eip" { defaults = { public_ip = "203.0.113.10" } }
}

variables {
  vpc_id = "vpc-00000000000000001"
  nodes = {
    platform-01 = { availability_zone = "eu-west-2a", subnet_id = "subnet-00000000000000001", private_ip = "10.40.0.20", wireguard_ip = "10.200.0.64" }
    platform-02 = { availability_zone = "eu-west-2b", subnet_id = "subnet-00000000000000002", private_ip = "10.40.1.20", wireguard_ip = "10.200.0.65" }
    platform-03 = { availability_zone = "eu-west-2c", subnet_id = "subnet-00000000000000003", private_ip = "10.40.2.20", wireguard_ip = "10.200.0.66" }
  }
  ami_id                   = "ami-057b46c043e81dcf4"
  permissions_boundary_arn = "arn:aws:iam::127811062476:policy/kubernetes-llm-inference-platform/kubernetes-llm-control-plane-boundary"
  ssh_public_key           = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIG8K44GFhFQd1dWoZLXK9Q6Zv3hNqqEPJfcQ4LfZxZxm offline-test"
  tags                     = { Project = "kubernetes-llm-inference-platform" }
}

run "protected_worker_capacity" {
  command = plan
  assert {
    condition     = length(aws_instance.node) == 3 && alltrue([for host in values(aws_instance.node) : host.disable_api_termination && host.metadata_options[0].http_tokens == "required" && host.metadata_options[0].http_put_response_hop_limit == 1 && host.root_block_device[0].encrypted && host.root_block_device[0].volume_size == 80])
    error_message = "Three protected workers must use encrypted scratch and IMDSv2 hop limit one."
  }
  assert {
    condition     = aws_iam_role_policy_attachment.ssm.policy_arn == "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
    error_message = "Persistent hosts must not inherit application or storage-controller privileges."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.wireguard.from_port == 51820 && aws_vpc_security_group_ingress_rule.wireguard.ip_protocol == "udp" && aws_vpc_security_group_ingress_rule.pmtu.ip_protocol == "icmp"
    error_message = "Initial ingress is limited to authenticated WireGuard and path-MTU ICMP."
  }
}

run "reject_incomplete_ha_tier" {
  command = plan
  variables {
    nodes = {
      platform-01 = { availability_zone = "eu-west-2a", subnet_id = "subnet-00000000000000001", private_ip = "10.40.0.20", wireguard_ip = "10.200.0.64" }
    }
  }
  expect_failures = [aws_security_group.platform]
}
