locals {
  cpu_profiles = {
    "m7i.xlarge"  = { core_count = 2, threads_per_core = 2 }
    "m7i.2xlarge" = { core_count = 4, threads_per_core = 2 }
  }
  egress = {
    http      = { protocol = "tcp", port = 80 }
    https     = { protocol = "tcp", port = 443 }
    wireguard = { protocol = "udp", port = 51820 }
  }
}

resource "aws_security_group" "platform" {
  name        = "klip-platform-workers"
  description = "Persistent platform workers; direct WireGuard, no public SSH or API"
  vpc_id      = var.vpc_id
  tags        = var.tags
  lifecycle {
    precondition {
      condition     = length(var.nodes) == 3 && length(distinct([for node in values(var.nodes) : node.availability_zone])) == 3
      error_message = "The HA platform tier requires three worker hosts across three availability zones."
    }
    precondition {
      condition     = length(distinct([for node in values(var.nodes) : node.private_ip])) == 3 && length(distinct([for node in values(var.nodes) : node.wireguard_ip])) == 3
      error_message = "Platform host and WireGuard addresses must be unique."
    }
    precondition {
      condition     = alltrue([for name, node in var.nodes : node.wireguard_ip == "10.200.0.${64 + index(sort(keys(var.nodes)), name)}"])
      error_message = "Platform hosts must use reserved mesh slots .64-.66, distinct from control planes and dynamic GPUs."
    }
  }
}

resource "aws_vpc_security_group_ingress_rule" "wireguard" {
  security_group_id = aws_security_group.platform.id
  ip_protocol       = "udp"
  from_port         = 51820
  to_port           = 51820
  cidr_ipv4         = "0.0.0.0/0"
  tags              = var.tags
}

resource "aws_vpc_security_group_ingress_rule" "pmtu" {
  security_group_id = aws_security_group.platform.id
  ip_protocol       = "icmp"
  from_port         = 3
  to_port           = 4
  cidr_ipv4         = "0.0.0.0/0"
  tags              = var.tags
}

resource "aws_vpc_security_group_egress_rule" "packages_and_mesh" {
  for_each          = local.egress
  security_group_id = aws_security_group.platform.id
  ip_protocol       = each.value.protocol
  from_port         = each.value.port
  to_port           = each.value.port
  cidr_ipv4         = "0.0.0.0/0"
  tags              = var.tags
}

resource "aws_iam_role" "platform" {
  name                 = "klip-platform-worker"
  path                 = "/kubernetes-llm-inference-platform/"
  description          = "Persistent worker SSM identity; no application, storage-controller or provider privileges"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.platform.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "platform" {
  name = "klip-platform-worker"
  path = "/kubernetes-llm-inference-platform/"
  role = aws_iam_role.platform.name
  tags = var.tags
}

resource "aws_key_pair" "platform" {
  key_name   = "klip-platform-worker"
  public_key = trimspace(var.ssh_public_key)
  tags       = var.tags
}

resource "aws_network_interface" "node" {
  for_each          = var.nodes
  subnet_id         = each.value.subnet_id
  private_ips       = [each.value.private_ip]
  security_groups   = [aws_security_group.platform.id]
  source_dest_check = false
  tags              = merge(var.tags, { Name = "klip-${each.key}", Node = each.key })
}

resource "aws_instance" "node" {
  for_each                = var.nodes
  ami                     = var.ami_id
  availability_zone       = each.value.availability_zone
  instance_type           = coalesce(each.value.instance_type, var.instance_type)
  iam_instance_profile    = aws_iam_instance_profile.platform.name
  key_name                = aws_key_pair.platform.key_name
  disable_api_termination = var.deletion_protection
  # EC2 preserves supported old CPU options across a type change. Explicit
  # profiles ensure an eight-vCPU worker actually exposes all eight vCPUs.
  cpu_options {
    core_count       = local.cpu_profiles[coalesce(each.value.instance_type, var.instance_type)].core_count
    threads_per_core = local.cpu_profiles[coalesce(each.value.instance_type, var.instance_type)].threads_per_core
  }
  primary_network_interface {
    network_interface_id = aws_network_interface.node[each.key].id
  }
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }
  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gib
    encrypted             = true
    delete_on_termination = true
    tags                  = merge(var.tags, { Name = "klip-${each.key}-root", Node = each.key })
  }
  tags = merge(var.tags, { Name = "klip-${each.key}", Node = each.key })
  # Reserve every required address before launching paid instances. A quota race
  # must not start compute while an address reservation is still failing.
  depends_on = [aws_iam_role_policy_attachment.ssm, aws_eip.node]
  lifecycle {
    ignore_changes = [source_dest_check]
  }
}

resource "aws_eip" "node" {
  for_each = var.nodes
  domain   = "vpc"
  tags     = merge(var.tags, { Name = "klip-${each.key}", Node = each.key })
}

resource "aws_eip_association" "node" {
  for_each             = var.nodes
  allocation_id        = aws_eip.node[each.key].id
  network_interface_id = aws_network_interface.node[each.key].id
  depends_on           = [aws_instance.node]
}
