variable "hosted_zone_id" {
  description = "Existing registration-created public hosted zone, never recreated here."
  type        = string
}
variable "hostname" {
  description = "The inference hostname within the existing public zone."
  type        = string
}
variable "origins" {
  description = "Foundation-reviewed, active Lambda gateway public IPs; empty until serving exists."
  type = map(object({
    ipv4   = string
    weight = optional(number, 1)
  }))
  default = {}
  validation {
    condition = alltrue([for key, origin in var.origins :
      can(regex("^[a-z0-9]+(-[a-z0-9]+)*$", key)) &&
      origin.weight >= 1 && origin.weight <= 255 && floor(origin.weight) == origin.weight
    ])
    error_message = "Origin IDs must be stable kebab-case names with integer weights from 1 to 255. Remove an origin instead of using zero weight."
  }
  validation {
    condition     = length(distinct([for origin in values(var.origins) : origin.ipv4])) == length(var.origins)
    error_message = "Each gateway public IP must occur only once."
  }
}
variable "health_path" {
  description = "Future gateway readiness path, independent of GPU allocation status."
  type        = string
  default     = "/readyz"
  validation {
    condition     = startswith(var.health_path, "/") && !can(regex("[\\r\\n]", var.health_path))
    error_message = "The health path must begin with / and contain no line breaks."
  }
}
variable "tags" {
  description = "Project ownership tags for health checks."
  type        = map(string)
  validation {
    condition     = try(var.tags.Project == "kubernetes-llm-inference-platform", false)
    error_message = "The Project ownership tag is required."
  }
}
