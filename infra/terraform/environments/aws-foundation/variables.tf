variable "ssh_public_key" {
  description = "Reviewed local Ed25519 PUBLIC key supplied by the operator wrapper."
  type        = string
}
variable "deletion_protection" {
  description = "True during normal operation; disable only through decommissioning."
  type        = bool
  default     = true
}
variable "reserved_cidrs" {
  description = "Additional local or provider ranges to exclude from the selected networks."
  type        = list(string)
  default     = []
}
