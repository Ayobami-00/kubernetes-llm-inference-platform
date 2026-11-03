plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "aws" {
  enabled = true
  version = "0.49.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}

# Shared repo-local modules are documented by the infrastructure runbook.
rule "terraform_standard_module_structure" {
  enabled = false
}
