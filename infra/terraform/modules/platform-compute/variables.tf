variable "vpc_id" {
  description = "Existing, project-owned foundation VPC."
  type        = string
}
variable "nodes" {
  description = "Persistent platform slots using existing CPU subnets and reserved worker mesh addresses."
  type = map(object({
    availability_zone = string
    subnet_id         = string
    private_ip        = string
    wireguard_ip      = string
    instance_type     = optional(string)
  }))
  validation {
    condition     = alltrue([for name in keys(var.nodes) : can(regex("^platform-0[123]$", name))])
    error_message = "Platform allocation cannot target a control-plane or arbitrary worker slot."
  }
}
variable "ami_id" {
  description = "Reviewed immutable Ubuntu AMD64 AMI from the existing project definition."
  type        = string
  validation {
    condition     = can(regex("^ami-[0-9a-f]{17}$", var.ami_id))
    error_message = "Supply an immutable AMI ID."
  }
}
variable "instance_type" {
  description = "Reviewed on-demand worker size; no automatic scaling or substitution."
  type        = string
  default     = "m7i.xlarge"
}
variable "root_volume_gib" {
  description = "Encrypted runtime/image scratch per worker; PostgreSQL data uses separate CSI volumes."
  type        = number
  default     = 80
  validation {
    condition     = var.root_volume_gib >= 40 && floor(var.root_volume_gib) == var.root_volume_gib
    error_message = "Use an integer root volume size of at least 40 GiB."
  }
}
variable "permissions_boundary_arn" {
  description = "Existing bootstrap-managed node boundary. The worker attaches only SSM core."
  type        = string
}
variable "ssh_public_key" {
  description = "Public administration key only. SSH transport uses SSM, not public port 22."
  type        = string
  validation {
    condition     = can(regex("^ssh-ed25519 [A-Za-z0-9+/=]+(?: .*)?$", trimspace(var.ssh_public_key)))
    error_message = "Supply an Ed25519 public key, never private material."
  }
}
variable "deletion_protection" {
  description = "Disable only in the reviewed drain/backup/teardown workflow."
  type        = bool
  default     = true
}
variable "tags" {
  description = "Project ownership and environment tags."
  type        = map(string)
  validation {
    condition     = try(var.tags.Project == "kubernetes-llm-inference-platform", false)
    error_message = "The project ownership tag is mandatory."
  }
}
