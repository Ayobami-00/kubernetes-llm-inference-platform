locals {
  config     = jsondecode(file("${path.module}/../../project.json"))
  telemetry  = jsondecode(file("${path.module}/../../observability.json"))
  identity   = data.terraform_remote_state.identity.outputs.identity_manifest
  topic_name = "klip-platform-alerts"
  alarm_name = "klip-management-external-unavailable"
  alarm_arn  = "arn:aws:cloudwatch:${local.telemetry.global_monitoring_region}:${local.config.account_id}:alarm:${local.alarm_name}"
  tags = {
    Project     = local.config.project, Cluster = local.config.cluster_id,
    Environment = local.config.environment, ManagedBy = "terraform-platform-observability"
  }
}

provider "aws" {
  region              = local.config.region
  allowed_account_ids = [local.config.account_id]
}
# Route 53 health-check metrics and their alarms live in us-east-1. This does
# not relocate applications, object storage, platform workers or GPU capacity.
provider "aws" {
  alias               = "global_monitoring"
  region              = local.telemetry.global_monitoring_region
  allowed_account_ids = [local.config.account_id]
}
data "terraform_remote_state" "identity" {
  backend = "s3"
  config = {
    bucket = local.config.state_bucket, key = "workload-identity/terraform.tfstate",
    region = local.config.region, encrypt = true
  }
}
variable "alert_email" {
  description = "User-authorized operational destination; private operator input, never committed."
  type        = string
  sensitive   = true
  validation {
    condition     = can(regex("^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}$", var.alert_email)) && length(var.alert_email) <= 254
    error_message = "Supply the authorized email destination without whitespace or control characters."
  }
}
resource "terraform_data" "identity_guard" {
  input = local.identity.provider_arn
  lifecycle {
    precondition {
      condition     = local.identity.account_id == local.config.account_id && local.identity.region == local.config.region && local.identity.cluster_id == local.config.cluster_id && local.identity.project == local.config.project && local.identity.issuer == "https://oidc.${local.config.domain}" && local.identity.audience == "sts.amazonaws.com" && local.identity.provider_arn == "arn:aws:iam::${local.config.account_id}:oidc-provider/oidc.${local.config.domain}"
      error_message = "Telemetry federation requires this project's verified retained OIDC provider."
    }
    precondition {
      condition     = local.telemetry.schema_version == "klip-observability.v1" && local.telemetry.namespace == "observability" && local.telemetry.global_monitoring_region == "us-east-1" && local.telemetry.management_health_path == "/health/ready" && toset(keys(local.telemetry.retention_days)) == toset(["metrics", "logs", "traces"]) && alltrue([for days in values(local.telemetry.retention_days) : days > 0 && days <= 30]) && local.telemetry.noncurrent_version_days == 7 && length(local.telemetry.service_accounts) == 6 && length(distinct([for sa in values(local.telemetry.service_accounts) : sa.name])) == 6
      error_message = "Review changes to telemetry scope, exact workload identities, retention or external monitoring region."
    }
  }
}
resource "aws_s3_bucket" "telemetry" {
  for_each      = local.telemetry.retention_days
  bucket        = "klip-${each.key}-${local.config.account_id}-${local.config.region}"
  force_destroy = false
  tags          = local.tags
  depends_on    = [terraform_data.identity_guard]
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket_public_access_block" "telemetry" {
  for_each                = aws_s3_bucket.telemetry
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_ownership_controls" "telemetry" {
  for_each = aws_s3_bucket.telemetry
  bucket   = each.value.id
  rule { object_ownership = "BucketOwnerEnforced" }
}
resource "aws_s3_bucket_versioning" "telemetry" {
  for_each = aws_s3_bucket.telemetry
  bucket   = each.value.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "telemetry" {
  for_each = aws_s3_bucket.telemetry
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_lifecycle_configuration" "telemetry" {
  for_each = aws_s3_bucket.telemetry
  bucket   = each.value.id
  rule {
    id     = "retain-recoverable-versions"
    status = "Enabled"
    filter {}
    # Engine retention owns CURRENT blocks, indexes and tombstones. Independent
    # current-object expiry can corrupt queries or delete still-required indexes.
    noncurrent_version_expiration { noncurrent_days = local.telemetry.noncurrent_version_days }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
  depends_on = [aws_s3_bucket_versioning.telemetry]
}
resource "aws_s3_bucket_policy" "telemetry" {
  for_each = aws_s3_bucket.telemetry
  bucket   = each.value.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Sid       = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "s3:*",
    Resource  = [each.value.arn, "${each.value.arn}/*"],
    Condition = { Bool = { "aws:SecureTransport" = "false" } }
  }] })
}
resource "aws_iam_role" "telemetry" {
  for_each             = local.telemetry.service_accounts
  name                 = "klip-observability-${each.key}"
  path                 = "/${local.config.project}/"
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = local.identity.provider_arn },
    Condition = { StringEquals = {
      "oidc.${local.config.domain}:sub" = "system:serviceaccount:${local.telemetry.namespace}:${each.value.name}",
      "oidc.${local.config.domain}:aud" = local.identity.audience
    } }
  }] })
  tags       = local.tags
  depends_on = [terraform_data.identity_guard]
  lifecycle { prevent_destroy = true }
}
resource "aws_iam_role_policy" "objects" {
  for_each = { for name, sa in local.telemetry.service_accounts : name => sa if sa.bucket != null }
  name     = "own-telemetry-bucket-only"
  role     = aws_iam_role.telemetry[each.key].id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["s3:GetBucketLocation", "s3:ListBucket"], Resource = aws_s3_bucket.telemetry[each.value.bucket].arn },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.telemetry[each.value.bucket].arn}/*" }
    ], each.value.access != "read" ? [
    { Effect = "Allow", Action = ["s3:ListBucketMultipartUploads"], Resource = aws_s3_bucket.telemetry[each.value.bucket].arn },
    { Effect = "Allow", Action = ["s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"], Resource = "${aws_s3_bucket.telemetry[each.value.bucket].arn}/*" }
    ] : [], each.value.access == "compact" ? [
    { Effect = "Allow", Action = ["s3:DeleteObject"], Resource = "${aws_s3_bucket.telemetry[each.value.bucket].arn}/*" }
  ] : []) })
}
output "observability_manifest" {
  description = "Allocated storage/identity/alerts, not evidence of a deployed or healthy telemetry stack."
  value = {
    schema_version   = "klip-observability-allocation.v1",
    account_id       = local.config.account_id, region = local.config.region, project = local.config.project,
    cluster_id       = local.config.cluster_id, namespace = local.telemetry.namespace,
    buckets          = { for name, bucket in aws_s3_bucket.telemetry : name => { name = bucket.id, retention_days = local.telemetry.retention_days[name] } },
    service_accounts = { for name, sa in local.telemetry.service_accounts : name => { name = sa.name, role_arn = aws_iam_role.telemetry[name].arn } },
    alerts = {
      region     = local.telemetry.global_monitoring_region, topic_arn = aws_sns_topic.alerts.arn,
      key_arn    = aws_kms_key.alerts.arn, health_check_id = aws_route53_health_check.management.id,
      hostname   = "management.${local.config.domain}", path = local.telemetry.management_health_path,
      alarm_name = aws_cloudwatch_metric_alarm.management.alarm_name, alarm_arn = aws_cloudwatch_metric_alarm.management.arn
    }
  }
}
