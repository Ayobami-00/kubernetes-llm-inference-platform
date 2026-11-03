locals {
  config     = jsondecode(file("${path.module}/../../project.json"))
  frontend   = jsondecode(file("${path.module}/../../frontend.json"))
  foundation = data.terraform_remote_state.foundation.outputs.foundation_manifest
  platform   = data.terraform_remote_state.platform.outputs.platform_manifest
  tags = {
    Project     = local.config.project, Cluster = local.config.cluster_id,
    Environment = local.config.environment, ManagedBy = "terraform-platform-frontend"
  }
}

provider "aws" {
  region              = local.config.region
  allowed_account_ids = [local.config.account_id]
}
data "terraform_remote_state" "foundation" {
  backend = "s3"
  config = {
    bucket = local.config.state_bucket, key = "aws-foundation/terraform.tfstate",
    region = local.config.region, encrypt = true
  }
}
data "terraform_remote_state" "platform" {
  backend = "s3"
  config = {
    bucket = local.config.state_bucket, key = "platform-workers/terraform.tfstate",
    region = local.config.region, encrypt = true
  }
}
variable "publish_dns" {
  description = "Activation requires fresh target-health and authenticated HTTPS evidence."
  type        = bool
  default     = false
}
variable "deletion_protection" {
  description = "Disable only for reviewed project frontend decommission/rebuild."
  type        = bool
  default     = true
}
resource "terraform_data" "identity_guard" {
  input = local.foundation.cluster_id
  lifecycle {
    precondition {
      condition = alltrue([for identity in [local.foundation, local.platform] :
        identity.account_id == local.config.account_id && identity.region == local.config.region && identity.cluster_id == local.config.cluster_id && identity.project == local.config.project
      ]) && local.foundation.network.vpc_id == local.platform.network.vpc_id && length(local.platform.nodes) == 3 && length(distinct([for n in local.platform.nodes : n.availability_zone])) == 3
      error_message = "Frontend requires this project's three-zone persistent worker allocation."
    }
    precondition {
      condition     = local.frontend.hostname == "management.${local.config.domain}" && local.frontend.node_port == 30443 && local.frontend.tls_policy == "ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09"
      error_message = "Management hostname, HTTPS backend port and TLS policy require explicit review."
    }
  }
}
resource "aws_security_group" "frontend" {
  name        = "klip-management-frontend"
  description = "Public management HTTPS; backend restricted to persistent worker group"
  vpc_id      = local.foundation.network.vpc_id
  tags        = local.tags
  depends_on  = [terraform_data.identity_guard]
}
resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.frontend.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
  tags              = local.tags
}
resource "aws_vpc_security_group_egress_rule" "backend" {
  security_group_id            = aws_security_group.frontend.id
  referenced_security_group_id = local.platform.network.security_group_id
  ip_protocol                  = "tcp"
  from_port                    = local.frontend.node_port
  to_port                      = local.frontend.node_port
  tags                         = local.tags
}
resource "aws_vpc_security_group_ingress_rule" "nodeport" {
  security_group_id            = local.platform.network.security_group_id
  referenced_security_group_id = aws_security_group.frontend.id
  ip_protocol                  = "tcp"
  from_port                    = local.frontend.node_port
  to_port                      = local.frontend.node_port
  tags                         = local.tags
}
resource "aws_lb" "management" {
  name                       = "klip-management"
  internal                   = false
  load_balancer_type         = "application"
  ip_address_type            = "ipv4"
  security_groups            = [aws_security_group.frontend.id]
  subnets                    = [for name in sort(keys(local.foundation.network.cpu_subnet_ids)) : local.foundation.network.cpu_subnet_ids[name]]
  enable_deletion_protection = var.deletion_protection
  drop_invalid_header_fields = true
  desync_mitigation_mode     = "strictest"
  idle_timeout               = 90
  client_keep_alive          = 3600
  tags                       = local.tags
}
resource "aws_lb_target_group" "web" {
  name                              = "klip-management-web"
  vpc_id                            = local.foundation.network.vpc_id
  port                              = local.frontend.node_port
  protocol                          = "HTTPS"
  protocol_version                  = "HTTP1"
  target_type                       = "instance"
  deregistration_delay              = 30
  load_balancing_cross_zone_enabled = "true"
  health_check {
    enabled             = true
    protocol            = "HTTPS"
    path                = "/health/ready"
    port                = "traffic-port"
    matcher             = "200"
    interval            = 10
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }
  tags = local.tags
}
resource "aws_lb_target_group_attachment" "nodes" {
  for_each         = local.platform.nodes
  target_group_arn = aws_lb_target_group.web.arn
  target_id        = each.value.instance_id
  port             = local.frontend.node_port
}
resource "aws_acm_certificate" "management" {
  domain_name       = local.frontend.hostname
  validation_method = "DNS"
  key_algorithm     = "EC_prime256v1"
  tags              = local.tags
  lifecycle { create_before_destroy = true }
}
resource "aws_route53_record" "validation" {
  for_each = { for v in aws_acm_certificate.management.domain_validation_options : v.domain_name => v }
  zone_id  = local.config.hosted_zone_id
  name     = each.value.resource_record_name
  type     = each.value.resource_record_type
  ttl      = 300
  records  = [each.value.resource_record_value]
}
resource "aws_acm_certificate_validation" "management" {
  certificate_arn         = aws_acm_certificate.management.arn
  validation_record_fqdns = [for v in aws_route53_record.validation : v.fqdn]
  timeouts { create = "15m" }
}
resource "aws_lb_listener" "https" {
  load_balancer_arn                                            = aws_lb.management.arn
  port                                                         = 443
  protocol                                                     = "HTTPS"
  certificate_arn                                              = aws_acm_certificate_validation.management.certificate_arn
  ssl_policy                                                   = local.frontend.tls_policy
  routing_http_response_server_enabled                         = false
  routing_http_response_strict_transport_security_header_value = "max-age=31536000"
  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "Not found"
      status_code  = "404"
    }
  }
}
resource "aws_lb_listener_rule" "management" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 10
  condition {
    host_header { values = [local.frontend.hostname] }
  }
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
  tags = local.tags
}
resource "aws_wafv2_web_acl" "management" {
  name  = "klip-management"
  scope = "REGIONAL"
  default_action {
    allow {}
  }
  custom_response_body {
    key          = "throttled"
    content_type = "APPLICATION_JSON"
    content      = jsonencode({ error = "rate_limited" })
  }
  dynamic "rule" {
    for_each = {
      all   = { priority = 10, limit = local.frontend.request_limit_per_5_minutes }
      login = { priority = 20, limit = local.frontend.login_limit_per_5_minutes }
    }
    content {
      name     = rule.key
      priority = rule.value.priority
      action {
        block {
          custom_response {
            response_code            = 429
            custom_response_body_key = "throttled"
          }
        }
      }
      statement {
        rate_based_statement {
          limit                 = rule.value.limit
          aggregate_key_type    = "IP"
          evaluation_window_sec = 300
          dynamic "scope_down_statement" {
            for_each = rule.key == "login" ? [1] : []
            content {
              byte_match_statement {
                positional_constraint = "EXACTLY"
                search_string         = "/api/v1/auth/login"
                field_to_match {
                  uri_path {}
                }
                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
          }
        }
      }
      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = "klip-management-${rule.key}"
        sampled_requests_enabled   = false
      }
    }
  }
  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "klip-management"
    sampled_requests_enabled   = false
  }
  tags = local.tags
}
resource "aws_wafv2_web_acl_association" "management" {
  resource_arn = aws_lb.management.arn
  web_acl_arn  = aws_wafv2_web_acl.management.arn
}
resource "aws_route53_record" "management" {
  count   = var.publish_dns ? 1 : 0
  zone_id = local.config.hosted_zone_id
  name    = local.frontend.hostname
  type    = "A"
  alias {
    name                   = aws_lb.management.dns_name
    zone_id                = aws_lb.management.zone_id
    evaluate_target_health = true
  }
  depends_on = [aws_lb_listener_rule.management, aws_wafv2_web_acl_association.management]
}
output "frontend_manifest" {
  value = {
    schema_version    = "klip-management-frontend.v1", project = local.config.project,
    account_id        = local.config.account_id, region = local.config.region, cluster_id = local.config.cluster_id,
    hostname          = local.frontend.hostname, origin = "https://${local.frontend.hostname}", published = var.publish_dns,
    load_balancer_arn = aws_lb.management.arn, dns_name = aws_lb.management.dns_name,
    target_group_arn  = aws_lb_target_group.web.arn, security_group_id = aws_security_group.frontend.id,
    certificate_arn   = aws_acm_certificate.management.arn, web_acl_arn = aws_wafv2_web_acl.management.arn,
    node_port         = local.frontend.node_port, nodes = { for name, node in local.platform.nodes : name => node.instance_id }
  }
}
