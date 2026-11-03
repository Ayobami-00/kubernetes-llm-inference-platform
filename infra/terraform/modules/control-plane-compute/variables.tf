variable "nodes" {
  description = "Stable node slots and their fixed addressing."
  type = map(object({
    availability_zone = string
    private_ip        = string
    wireguard_ip      = string
  }))
}
variable "subnet_ids" {
  description = "CPU subnets by node slot."
  type        = map(string)
}
variable "security_group_id" {
  description = "Control-plane transport security group."
  type        = string
}
variable "ami_id" {
  description = "Reviewed immutable Canonical Ubuntu AMI ID."
  type        = string
  validation {
    condition     = can(regex("^ami-[0-9a-f]{17}$", var.ami_id))
    error_message = "Supply an explicitly pinned AMI ID."
  }
}
variable "instance_type" {
  description = "On-demand CPU instance size."
  type        = string
  default     = "m7i.large"
}
variable "permissions_boundary_arn" {
  description = "Bootstrap-managed SSM-only node role boundary."
  type        = string
}
variable "ssh_public_key" {
  description = "Public SSH key only; private material never enters Terraform."
  type        = string
  validation {
    condition     = can(regex("^ssh-ed25519 [A-Za-z0-9+/=]+(?: .*)?$", trimspace(var.ssh_public_key)))
    error_message = "Supply an OpenSSH Ed25519 public key, never a private key."
  }
}
variable "deletion_protection" {
  description = "Disable only through the reviewed decommission workflow."
  type        = bool
  default     = true
}
variable "tags" {
  description = "Project ownership tags."
  type        = map(string)
  validation {
    condition     = try(var.tags.Project == "kubernetes-llm-inference-platform", false)
    error_message = "The Project ownership tag is required."
  }
}
