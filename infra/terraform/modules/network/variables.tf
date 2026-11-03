variable "vpc_cidr" {
  description = "Dedicated IPv4 VPC range."
  type        = string
}

variable "overlay_cidrs" {
  description = "WireGuard, Pod and Service networks, disjoint from the VPC."
  type        = list(string)
  validation {
    condition     = length(var.overlay_cidrs) == 3
    error_message = "Supply WireGuard, Pod and Service CIDRs in that order."
  }
}

variable "reserved_cidrs" {
  description = "Additional local/provider networks that must not overlap."
  type        = list(string)
  default     = []
}

variable "nodes" {
  description = "Exactly three stable control-plane slots, including API frontend addressing."
  type = map(object({
    availability_zone  = string
    public_subnet_cidr = string
    private_ip         = string
    api_subnet_cidr    = string
    api_ip             = string
    wireguard_ip       = string
  }))
  validation {
    condition     = toset(keys(var.nodes)) == toset(["cp-01", "cp-02", "cp-03"])
    error_message = "Use exactly the stable slots cp-01, cp-02 and cp-03."
  }
}

variable "tags" {
  description = "Ownership tags required by the scoped AWS role."
  type        = map(string)
  validation {
    condition     = try(var.tags.Project == "kubernetes-llm-inference-platform", false)
    error_message = "The Project ownership tag is required."
  }
}
