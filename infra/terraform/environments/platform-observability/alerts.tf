resource "aws_kms_key" "alerts" {
  provider                = aws.global_monitoring
  description             = "klip operational notifications; CloudWatch and exact Alertmanager workload only"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Sid       = "AccountKeyAdministration", Effect = "Allow",
      Principal = { AWS = "arn:aws:iam::${local.config.account_id}:root" }, Action = "kms:*", Resource = "*"
    },
    {
      Sid       = "OnlyThisExternalAlarm", Effect = "Allow", Principal = { Service = "cloudwatch.amazonaws.com" },
      Action    = ["kms:Decrypt", "kms:GenerateDataKey*"], Resource = "*",
      Condition = { StringEquals = { "aws:SourceAccount" = local.config.account_id }, ArnEquals = { "aws:SourceArn" = local.alarm_arn } }
    }
  ] })
  tags       = local.tags
  depends_on = [terraform_data.identity_guard]
  lifecycle { prevent_destroy = true }
}
resource "aws_kms_alias" "alerts" {
  provider      = aws.global_monitoring
  name          = "alias/klip-platform-alerts"
  target_key_id = aws_kms_key.alerts.key_id
}
resource "aws_sns_topic" "alerts" {
  provider          = aws.global_monitoring
  name              = local.topic_name
  kms_master_key_id = aws_kms_key.alerts.arn
  tags              = local.tags
  lifecycle { prevent_destroy = true }
}
resource "aws_sns_topic_policy" "alerts" {
  provider = aws.global_monitoring
  arn      = aws_sns_topic.alerts.arn
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Sid       = "AccountTopicAdministration", Effect = "Allow",
      Principal = { AWS = "arn:aws:iam::${local.config.account_id}:root" }, Action = "sns:*", Resource = aws_sns_topic.alerts.arn
    },
    {
      Sid       = "OnlyThisExternalAlarm", Effect = "Allow", Principal = { Service = "cloudwatch.amazonaws.com" },
      Action    = "sns:Publish", Resource = aws_sns_topic.alerts.arn,
      Condition = { StringEquals = { "aws:SourceAccount" = local.config.account_id }, ArnEquals = { "aws:SourceArn" = local.alarm_arn } }
    },
    {
      Sid       = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "sns:*", Resource = aws_sns_topic.alerts.arn,
      Condition = { Bool = { "aws:SecureTransport" = "false", "aws:PrincipalIsAWSService" = "false" } }
    }
  ] })
}
resource "aws_sns_topic_subscription" "email" {
  provider   = aws.global_monitoring
  topic_arn  = aws_sns_topic.alerts.arn
  protocol   = "email"
  endpoint   = var.alert_email
  depends_on = [aws_sns_topic_policy.alerts]
}
resource "aws_iam_role_policy" "notifications" {
  name = "publish-operational-alerts-only"
  role = aws_iam_role.telemetry["alertmanager"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["sns:Publish"], Resource = aws_sns_topic.alerts.arn },
    {
      Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey*"], Resource = aws_kms_key.alerts.arn,
      Condition = { StringEquals = {
        "kms:ViaService"                         = "sns.${local.telemetry.global_monitoring_region}.amazonaws.com",
        "kms:EncryptionContext:aws:sns:topicArn" = aws_sns_topic.alerts.arn
      } }
    }
  ] })
}
resource "aws_route53_health_check" "management" {
  type              = "HTTPS"
  fqdn              = "management.${local.config.domain}"
  port              = 443
  resource_path     = local.telemetry.management_health_path
  request_interval  = 30
  failure_threshold = 3
  enable_sni        = true
  regions           = local.telemetry.health_check_regions
  tags              = merge(local.tags, { Name = "klip-management-external-readiness" })
  depends_on        = [terraform_data.identity_guard]
}
resource "aws_cloudwatch_metric_alarm" "management" {
  provider            = aws.global_monitoring
  alarm_name          = local.alarm_name
  alarm_description   = "External management readiness is unavailable or its Route 53 metric is missing. Availability only: Route 53 does not validate TLS certificates."
  namespace           = "AWS/Route53"
  metric_name         = "HealthCheckStatus"
  dimensions          = { HealthCheckId = aws_route53_health_check.management.id }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = local.tags
  depends_on          = [aws_sns_topic_policy.alerts]
}
