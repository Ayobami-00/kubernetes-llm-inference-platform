mock_provider "aws" {}
run "isolated_expiring_probe" {
  command = plan
  variables {
    ssh_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockAcceptanceKey"
  }
  assert {
    condition     = aws_instance.probe.instance_initiated_shutdown_behavior == "terminate" && aws_instance.probe.metadata_options[0].http_tokens == "required"
    error_message = "Probe must expire and require IMDSv2."
  }
  assert {
    condition     = aws_vpc.probe.cidr_block == "10.41.0.0/24"
    error_message = "Probe must be outside the cluster VPC."
  }
}
