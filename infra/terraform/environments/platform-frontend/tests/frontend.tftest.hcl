mock_provider "aws" {}
override_resource {
  target          = aws_acm_certificate.management
  override_during = plan
  values = {
    arn = "arn:aws:acm:eu-west-2:127811062476:certificate/00000000-0000-0000-0000-000000000001"
    domain_validation_options = [{
      domain_name          = "management.klip-lab.click", resource_record_name = "_test.management.klip-lab.click",
      resource_record_type = "CNAME", resource_record_value = "_test.acm-validations.aws."
    }]
  }
}
override_data {
  target = data.terraform_remote_state.foundation
  values = {
    outputs = {
      foundation_manifest = {
        account_id = "127811062476", region = "eu-west-2", cluster_id = "klip", project = "kubernetes-llm-inference-platform"
        network = {
          vpc_id         = "vpc-00000000000000001"
          cpu_subnet_ids = { one = "subnet-00000000000000001", two = "subnet-00000000000000002", three = "subnet-00000000000000003" }
        }
      }
    }
  }
}
override_data {
  target = data.terraform_remote_state.platform
  values = {
    outputs = {
      platform_manifest = {
        account_id = "127811062476", region = "eu-west-2", cluster_id = "klip", project = "kubernetes-llm-inference-platform"
        network    = { vpc_id = "vpc-00000000000000001", security_group_id = "sg-00000000000000001" }
        nodes = {
          one   = { instance_id = "i-00000000000000001", availability_zone = "eu-west-2a" }
          two   = { instance_id = "i-00000000000000002", availability_zone = "eu-west-2b" }
          three = { instance_id = "i-00000000000000003", availability_zone = "eu-west-2c" }
        }
      }
    }
  }
}

run "unpublished_encrypted_frontend" {
  command = plan
  assert {
    condition     = length(aws_route53_record.management) == 0 && !output.frontend_manifest.published
    error_message = "Management DNS must remain unpublished until verified activation."
  }
  assert {
    condition     = aws_lb.management.enable_deletion_protection && aws_lb.management.drop_invalid_header_fields && aws_lb.management.desync_mitigation_mode == "strictest" && length(aws_lb.management.subnets) == 3
    error_message = "The public frontend needs three-zone placement and defensive protocol handling."
  }
  assert {
    condition     = aws_lb_listener.https.port == 443 && aws_lb_target_group.web.protocol == "HTTPS" && length(aws_lb_target_group_attachment.nodes) == 3 && aws_lb_target_group.web.health_check[0].path == "/health/ready"
    error_message = "Only encrypted, readiness-checked management endpoints may receive traffic."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.nodeport.from_port == 30443 && aws_vpc_security_group_egress_rule.backend.to_port == 30443 && aws_vpc_security_group_ingress_rule.https.from_port == 443 && aws_vpc_security_group_ingress_rule.nodeport.cidr_ipv4 == null
    error_message = "The backend port must be accessible only through its frontend security group."
  }
  assert {
    condition     = length(aws_wafv2_web_acl.management.rule) == 2 && !aws_wafv2_web_acl.management.visibility_config[0].sampled_requests_enabled
    error_message = "Both frontend/login IP rate controls are required without retained request samples."
  }
}

run "explicit_publication" {
  command = plan
  variables { publish_dns = true }
  assert {
    condition     = length(aws_route53_record.management) == 1 && aws_route53_record.management[0].alias[0].evaluate_target_health
    error_message = "The activated hostname must follow the health-aware managed ALB alias."
  }
}

run "reject_collapsed_failure_domains" {
  command = plan
  override_data {
    target = data.terraform_remote_state.platform
    values = {
      outputs = {
        platform_manifest = {
          account_id = "127811062476", region = "eu-west-2", cluster_id = "klip", project = "kubernetes-llm-inference-platform"
          network    = { vpc_id = "vpc-00000000000000001", security_group_id = "sg-00000000000000001" }
          nodes = {
            one   = { instance_id = "i-00000000000000001", availability_zone = "eu-west-2a" }
            two   = { instance_id = "i-00000000000000002", availability_zone = "eu-west-2a" }
            three = { instance_id = "i-00000000000000003", availability_zone = "eu-west-2a" }
          }
        }
      }
    }
  }
  expect_failures = [terraform_data.identity_guard]
}
