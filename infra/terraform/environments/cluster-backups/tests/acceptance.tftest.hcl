mock_provider "aws" {}
run "retained_private_backups" {
  command = plan
  assert {
    condition     = !aws_s3_bucket.backups.force_destroy && aws_s3_bucket_public_access_block.backups.block_public_policy
    error_message = "Recovery bucket must be retained and private."
  }
  assert {
    condition     = one(aws_s3_bucket_lifecycle_configuration.backups.rule).expiration[0].days == 30
    error_message = "Backup retention must remain 30 days."
  }
}
