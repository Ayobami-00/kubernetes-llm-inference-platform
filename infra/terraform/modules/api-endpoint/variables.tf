variable "vpc_id" {
  description = "Foundation VPC."
  type        = string
}
variable "frontends" {
  description = "Three private NLB frontends by stable slot."
  type        = map(object({ subnet_id = string, private_ip = string }))
  validation {
    condition     = length(var.frontends) == 3
    error_message = "The API endpoint requires three frontend subnets."
  }
}
variable "backend_ips" {
  description = "Control-plane native private IPs, not WireGuard or public IPs."
  type        = map(string)
  validation {
    condition     = length(var.backend_ips) == 3 && length(distinct(values(var.backend_ips))) == 3
    error_message = "Exactly three distinct control-plane targets are required."
  }
}
variable "security_group_id" {
  description = "Network-owned private API security group."
  type        = string
}
variable "deletion_protection" {
  description = "Disable only during reviewed foundation decommissioning."
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
