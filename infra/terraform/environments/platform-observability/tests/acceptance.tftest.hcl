mock_provider "aws" {}
mock_provider "aws" { alias = "global_monitoring" }
override_resource {
  override_during = plan
  target          = aws_s3_bucket.telemetry["metrics"]
  values          = { arn = "arn:aws:s3:::klip-metrics-127811062476-eu-west-2" }
}
override_resource {
  override_during = plan
  target          = aws_s3_bucket.telemetry["logs"]
  values          = { arn = "arn:aws:s3:::klip-logs-127811062476-eu-west-2" }
}
override_resource {
  override_during = plan
  target          = aws_s3_bucket.telemetry["traces"]
  values          = { arn = "arn:aws:s3:::klip-traces-127811062476-eu-west-2" }
}
override_data {
  target = data.terraform_remote_state.identity
  values = {
    outputs = {
      identity_manifest = {
        account_id   = "127811062476", region = "eu-west-2", cluster_id = "klip",
        project      = "kubernetes-llm-inference-platform", issuer = "https://oidc.klip-lab.click",
        audience     = "sts.amazonaws.com",
        provider_arn = "arn:aws:iam::127811062476:oidc-provider/oidc.klip-lab.click"
      }
    }
  }
}
variables { alert_email = "operator@example.invalid" }

run "retained_scoped_telemetry" {
  command = plan
  assert {
    condition     = length(aws_s3_bucket.telemetry) == 3 && alltrue([for bucket in aws_s3_bucket.telemetry : !bucket.force_destroy]) && alltrue([for version in aws_s3_bucket_versioning.telemetry : version.versioning_configuration[0].status == "Enabled"]) && alltrue([for encryption in aws_s3_bucket_server_side_encryption_configuration.telemetry : one(encryption.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "AES256"])
    error_message = "Retain three encrypted versioned buckets independently of replaceable cluster resources."
  }
  assert {
    condition     = alltrue([for policy in aws_s3_bucket_lifecycle_configuration.telemetry : length(one(policy.rule).expiration) == 0 && one(one(policy.rule).noncurrent_version_expiration).noncurrent_days == 7])
    error_message = "Engine-owned current blocks/indexes must not expire independently; preserve deleted-object recovery versions."
  }
  assert {
    condition     = alltrue([for name, role in aws_iam_role.telemetry : role.max_session_duration == 3600 && jsondecode(role.assume_role_policy).Statement[0].Condition.StringEquals["oidc.klip-lab.click:sub"] == "system:serviceaccount:observability:${local.telemetry.service_accounts[name].name}" && jsondecode(role.assume_role_policy).Statement[0].Condition.StringEquals["oidc.klip-lab.click:aud"] == "sts.amazonaws.com"])
    error_message = "Federation must bind exact namespace/account names and audience without host or wildcard fallback."
  }
  assert {
    condition     = !strcontains(aws_iam_role_policy.objects["prometheus"].policy, "s3:DeleteObject") && !strcontains(aws_iam_role_policy.objects["thanos-store"].policy, "s3:PutObject") && !strcontains(aws_iam_role_policy.objects["thanos-store"].policy, "s3:DeleteObject") && strcontains(aws_iam_role_policy.objects["thanos-compactor"].policy, "s3:DeleteObject") && !strcontains(aws_iam_role_policy.objects["loki"].policy, "DeleteObjectVersion")
    error_message = "Separate block writers, readers and compactors; no workload can erase retained object versions."
  }
  assert {
    condition     = aws_route53_health_check.management.fqdn == "management.klip-lab.click" && aws_route53_health_check.management.resource_path == "/health/ready" && aws_route53_health_check.management.type == "HTTPS" && aws_route53_health_check.management.enable_sni && aws_cloudwatch_metric_alarm.management.treat_missing_data == "breaching" && aws_cloudwatch_metric_alarm.management.evaluation_periods == 2 && aws_cloudwatch_metric_alarm.management.datapoints_to_alarm == 2 && aws_sns_topic_subscription.email.protocol == "email" && aws_kms_key.alerts.enable_key_rotation
    error_message = "External cluster-independent readiness, missing-data alarms and encrypted confirmed-email delivery are required."
  }
  assert {
    condition     = jsondecode(aws_kms_key.alerts.policy).Statement[1].Condition.ArnEquals["aws:SourceArn"] == local.alarm_arn && jsondecode(aws_kms_key.alerts.policy).Statement[1].Condition.StringEquals["aws:SourceAccount"] == "127811062476"
    error_message = "CloudWatch KMS use must bind this account and exact external alarm."
  }
}

run "reject_foreign_issuer" {
  command = plan
  override_data {
    target = data.terraform_remote_state.identity
    values = {
      outputs = {
        identity_manifest = {
          account_id   = "000000000000", region = "eu-west-2", cluster_id = "klip",
          project      = "kubernetes-llm-inference-platform", issuer = "https://oidc.klip-lab.click",
          audience     = "sts.amazonaws.com",
          provider_arn = "arn:aws:iam::000000000000:oidc-provider/oidc.klip-lab.click"
        }
      }
    }
  }
  expect_failures = [terraform_data.identity_guard]
}
run "reject_malformed_receiver" {
  command = plan
  variables { alert_email = "operator@example.invalid\nother@example.invalid" }
  expect_failures = [var.alert_email]
}
