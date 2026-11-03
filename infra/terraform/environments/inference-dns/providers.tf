provider "aws" {
  region              = local.config.region
  allowed_account_ids = [local.config.account_id]
  default_tags {
    tags = local.tags
  }
}
