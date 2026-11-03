mock_provider "aws" {
  mock_resource "aws_security_group" { defaults = { id = "sg-00000000000000001" } }
  mock_resource "aws_network_interface" { defaults = { id = "eni-00000000000000001" } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::127811062476:role/kubernetes-llm-inference-platform/klip-platform-worker" } }
  mock_resource "aws_instance" { defaults = { id = "i-00000000000000001" } }
  mock_resource "aws_eip" { defaults = { public_ip = "203.0.113.10" } }
}

override_data {
  target = data.terraform_remote_state.foundation
  values = {
    outputs = {
      foundation_manifest = {
        cluster_id = "klip", account_id = "127811062476", region = "eu-west-2", project = "kubernetes-llm-inference-platform"
        network = {
          vpc_id         = "vpc-00000000000000001"
          cpu_subnet_ids = { cp-01 = "subnet-00000000000000001", cp-02 = "subnet-00000000000000002", cp-03 = "subnet-00000000000000003" }
        }
        nodes = { cp-01 = {}, cp-02 = {}, cp-03 = {} }
      }
    }
  }
}

variables {
  ssh_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIG8K44GFhFQd1dWoZLXK9Q6Zv3hNqqEPJfcQ4LfZxZxm offline-test"
}

run "separate_worker_allocation" {
  command = plan
  assert {
    condition     = output.platform_manifest.cluster_id == "klip" && output.platform_manifest.instance_type == "m7i.xlarge" && length(output.platform_manifest.nodes) == 3
    error_message = "The additional allocation must preserve project identity and three worker slots."
  }
}

run "reject_foreign_foundation" {
  command = plan
  override_data {
    target = data.terraform_remote_state.foundation
    values = {
      outputs = {
        foundation_manifest = {
          cluster_id = "other", account_id = "999999999999", region = "eu-west-2", project = "other"
          network = {
            vpc_id         = "vpc-00000000000000001"
            cpu_subnet_ids = { cp-01 = "subnet-00000000000000001", cp-02 = "subnet-00000000000000002", cp-03 = "subnet-00000000000000003" }
          }
          nodes = { cp-01 = {}, cp-02 = {}, cp-03 = {} }
        }
      }
    }
  }
  expect_failures = [terraform_data.foundation_identity]
}
