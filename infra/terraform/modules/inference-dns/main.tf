locals {
  excluded_networks = [
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4"
  ]
  public_origins = alltrue([for origin in values(var.origins) : try(
    origin.ipv4 == cidrhost("${origin.ipv4}/32", 0) &&
    alltrue([for network in local.excluded_networks :
      cidrhost(network, 0) != cidrhost("${origin.ipv4}/${split("/", network)[1]}", 0)
    ]), false
  )])
}

data "aws_route53_zone" "existing" {
  zone_id      = var.hosted_zone_id
  private_zone = false
  lifecycle {
    postcondition {
      condition     = !self.private_zone && var.hostname == "inference.${trimsuffix(self.name, ".")}"
      error_message = "Use the existing public zone and its inference subdomain; do not replace apex, NS or SOA records."
    }
    postcondition {
      condition     = local.public_origins
      error_message = "Origins must be canonical, globally routable public IPv4 addresses, not documentation or private addresses."
    }
  }
}

resource "aws_route53_health_check" "gateway" {
  for_each          = var.origins
  ip_address        = each.value.ipv4
  fqdn              = var.hostname
  port              = 443
  type              = "HTTPS"
  resource_path     = var.health_path
  request_interval  = 30
  failure_threshold = 3
  enable_sni        = true
  measure_latency   = false
  tags              = merge(var.tags, { Name = "klip-inference-${each.key}" })
  depends_on        = [data.aws_route53_zone.existing]
}

resource "aws_route53_record" "gateway" {
  for_each        = var.origins
  zone_id         = data.aws_route53_zone.existing.zone_id
  name            = var.hostname
  type            = "A"
  ttl             = 30
  records         = [each.value.ipv4]
  set_identifier  = each.key
  health_check_id = aws_route53_health_check.gateway[each.key].id
  allow_overwrite = false
  weighted_routing_policy {
    weight = each.value.weight
  }
}
