locals {
  networks = concat([var.vpc_cidr], var.overlay_cidrs, var.reserved_cidrs)
  subnets  = flatten([for node in values(var.nodes) : [node.public_subnet_cidr, node.api_subnet_cidr]])
  valid_networks = alltrue([for cidr in concat(local.networks, local.subnets) : try(
    cidrnetmask(cidr) != "" && cidr == "${cidrhost(cidr, 0)}/${split("/", cidr)[1]}", false
  )])
  disjoint = alltrue(flatten([for i, a in local.networks : [for j, b in local.networks : try(
    cidrhost(a, 0) != cidrhost("${cidrhost(b, 0)}/${split("/", a)[1]}", 0) &&
    cidrhost(b, 0) != cidrhost("${cidrhost(a, 0)}/${split("/", b)[1]}", 0), false
  ) if i < j]]))
  subnets_disjoint = alltrue(flatten([for i, a in local.subnets : [for j, b in local.subnets : try(
    cidrhost(a, 0) != cidrhost("${cidrhost(b, 0)}/${split("/", a)[1]}", 0) &&
    cidrhost(b, 0) != cidrhost("${cidrhost(a, 0)}/${split("/", b)[1]}", 0), false
  ) if i < j]]))
  subnets_inside_vpc = alltrue([for cidr in local.subnets : try(
    tonumber(split("/", cidr)[1]) > tonumber(split("/", var.vpc_cidr)[1]) &&
    cidrhost(var.vpc_cidr, 0) == cidrhost("${cidrhost(cidr, 0)}/${split("/", var.vpc_cidr)[1]}", 0), false
  )])
  host_addresses_valid = alltrue([for n in values(var.nodes) : try(
    n.private_ip == cidrhost("${n.private_ip}/32", 0) &&
    n.api_ip == cidrhost("${n.api_ip}/32", 0) &&
    cidrhost(n.public_subnet_cidr, 0) == cidrhost("${n.private_ip}/${split("/", n.public_subnet_cidr)[1]}", 0) &&
    cidrhost(n.api_subnet_cidr, 0) == cidrhost("${n.api_ip}/${split("/", n.api_subnet_cidr)[1]}", 0) &&
    !contains([cidrhost(n.public_subnet_cidr, 0), cidrhost(n.public_subnet_cidr, 1), cidrhost(n.public_subnet_cidr, 2), cidrhost(n.public_subnet_cidr, 3), cidrhost(n.public_subnet_cidr, -1)], n.private_ip) &&
    !contains([cidrhost(n.api_subnet_cidr, 0), cidrhost(n.api_subnet_cidr, 1), cidrhost(n.api_subnet_cidr, 2), cidrhost(n.api_subnet_cidr, 3), cidrhost(n.api_subnet_cidr, -1)], n.api_ip), false
  )])
  wireguard_valid = length(distinct([for n in values(var.nodes) : n.wireguard_ip])) == 3 && alltrue([
    for n in values(var.nodes) : try(
      n.wireguard_ip == cidrhost("${n.wireguard_ip}/32", 0) &&
      cidrhost(var.overlay_cidrs[0], 0) == cidrhost("${n.wireguard_ip}/${split("/", var.overlay_cidrs[0])[1]}", 0) &&
    !contains([cidrhost(var.overlay_cidrs[0], 0), cidrhost(var.overlay_cidrs[0], -1)], n.wireguard_ip), false)
  ])
  internet_egress = {
    http      = { protocol = "tcp", port = 80 }
    https     = { protocol = "tcp", port = 443 }
    wireguard = { protocol = "udp", port = 51820 }
  }
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(var.tags, { Name = "klip" })
  lifecycle {
    precondition {
      condition     = local.valid_networks && local.disjoint
      error_message = "VPC, WireGuard, Pod, Service and reserved ranges must be canonical, non-overlapping IPv4 CIDRs."
    }
    precondition {
      condition     = local.subnets_disjoint && local.subnets_inside_vpc && local.host_addresses_valid && local.wireguard_valid
      error_message = "Subnets must be distinct and inside the VPC, with usable host addresses in their assigned subnets."
    }
    precondition {
      condition     = length(distinct([for node in values(var.nodes) : node.availability_zone])) == 3
      error_message = "Control-plane slots must use three distinct availability zones."
    }
  }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "klip" })
}

resource "aws_subnet" "cpu" {
  for_each                = var.nodes
  vpc_id                  = aws_vpc.this.id
  availability_zone       = each.value.availability_zone
  cidr_block              = each.value.public_subnet_cidr
  map_public_ip_on_launch = false
  tags                    = merge(var.tags, { Name = "klip-${each.key}" })
}

resource "aws_subnet" "api" {
  for_each                = var.nodes
  vpc_id                  = aws_vpc.this.id
  availability_zone       = each.value.availability_zone
  cidr_block              = each.value.api_subnet_cidr
  map_public_ip_on_launch = false
  tags                    = merge(var.tags, { Name = "klip-api-${each.key}" })
}

resource "aws_route_table" "cpu" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "klip-cpu" })
}

resource "aws_route" "internet" {
  route_table_id         = aws_route_table.cpu.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table" "api" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "klip-api-private" })
}

resource "aws_route_table_association" "cpu" {
  for_each       = var.nodes
  subnet_id      = aws_subnet.cpu[each.key].id
  route_table_id = aws_route_table.cpu.id
}

resource "aws_route_table_association" "api" {
  for_each       = var.nodes
  subnet_id      = aws_subnet.api[each.key].id
  route_table_id = aws_route_table.api.id
}

resource "aws_security_group" "cpu" {
  name        = "klip-control-plane"
  description = "WireGuard transport, SSM egress and API backend traffic"
  vpc_id      = aws_vpc.this.id
  tags        = var.tags
}

resource "aws_security_group" "api" {
  name        = "klip-api"
  description = "Private API access from AWS peers, including future SNATed mesh clients"
  vpc_id      = aws_vpc.this.id
  tags        = var.tags
}

resource "aws_vpc_security_group_ingress_rule" "wireguard" {
  security_group_id = aws_security_group.cpu.id
  description       = "Ephemeral peers authenticate with WireGuard keys; no AWS writes required"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "udp"
  from_port         = 51820
  to_port           = 51820
  tags              = var.tags
}

resource "aws_vpc_security_group_ingress_rule" "api_client" {
  security_group_id            = aws_security_group.api.id
  referenced_security_group_id = aws_security_group.cpu.id
  description                  = "CPU peers and future mesh traffic SNATed by those peers"
  ip_protocol                  = "tcp"
  from_port                    = 6443
  to_port                      = 6443
  tags                         = var.tags
}

resource "aws_vpc_security_group_ingress_rule" "api_backend" {
  security_group_id            = aws_security_group.cpu.id
  referenced_security_group_id = aws_security_group.api.id
  description                  = "Only the internal API NLB may reach native backend 6443"
  ip_protocol                  = "tcp"
  from_port                    = 6443
  to_port                      = 6443
  tags                         = var.tags
}

resource "aws_vpc_security_group_egress_rule" "internet" {
  for_each          = local.internet_egress
  security_group_id = aws_security_group.cpu.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = each.value.protocol
  from_port         = each.value.port
  to_port           = each.value.port
  description       = "${each.key} egress"
  tags              = var.tags
}

resource "aws_vpc_security_group_egress_rule" "dns" {
  for_each          = toset(["tcp", "udp"])
  security_group_id = aws_security_group.cpu.id
  cidr_ipv4         = "${cidrhost(var.vpc_cidr, 2)}/32"
  ip_protocol       = each.key
  from_port         = 53
  to_port           = 53
  description       = "VPC DNS resolver"
  tags              = var.tags
}

resource "aws_vpc_security_group_egress_rule" "api_client" {
  security_group_id            = aws_security_group.cpu.id
  referenced_security_group_id = aws_security_group.api.id
  ip_protocol                  = "tcp"
  from_port                    = 6443
  to_port                      = 6443
  tags                         = var.tags
}

resource "aws_vpc_security_group_egress_rule" "api_backend" {
  security_group_id            = aws_security_group.api.id
  referenced_security_group_id = aws_security_group.cpu.id
  ip_protocol                  = "tcp"
  from_port                    = 6443
  to_port                      = 6443
  tags                         = var.tags
}

resource "aws_vpc_security_group_ingress_rule" "pmtu" {
  for_each          = { cpu = aws_security_group.cpu.id, api = aws_security_group.api.id }
  security_group_id = each.value
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "icmp"
  from_port         = 3
  to_port           = 4
  description       = "IPv4 fragmentation-needed messages for path MTU discovery"
  tags              = var.tags
}

resource "aws_vpc_security_group_egress_rule" "pmtu" {
  for_each          = { cpu = aws_security_group.cpu.id, api = aws_security_group.api.id }
  security_group_id = each.value
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "icmp"
  from_port         = 3
  to_port           = 4
  description       = "IPv4 fragmentation-needed messages"
  tags              = var.tags
}
