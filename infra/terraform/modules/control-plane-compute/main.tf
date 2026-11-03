resource "aws_iam_role" "node" {
  name                 = "klip-control-plane"
  path                 = "/kubernetes-llm-inference-platform/"
  description          = "Control-plane host SSM agent; no application cloud permissions"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "backups" {
  role       = aws_iam_role.node.name
  policy_arn = replace(var.permissions_boundary_arn, "kubernetes-llm-control-plane-boundary", "kubernetes-llm-node-backup")
}

resource "aws_iam_instance_profile" "node" {
  name = "klip-control-plane"
  path = "/kubernetes-llm-inference-platform/"
  role = aws_iam_role.node.name
  tags = var.tags
}

resource "aws_key_pair" "foundation" {
  key_name   = "klip-foundation"
  public_key = trimspace(var.ssh_public_key)
  tags       = var.tags
}

resource "aws_network_interface" "node" {
  for_each          = var.nodes
  subnet_id         = var.subnet_ids[each.key]
  private_ips       = [each.value.private_ip]
  security_groups   = [var.security_group_id]
  source_dest_check = false
  tags              = merge(var.tags, { Name = "klip-${each.key}", Node = each.key })
}

resource "aws_instance" "node" {
  for_each                = var.nodes
  ami                     = var.ami_id
  instance_type           = var.instance_type
  iam_instance_profile    = aws_iam_instance_profile.node.name
  key_name                = aws_key_pair.foundation.key_name
  disable_api_termination = var.deletion_protection
  monitoring              = false
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
    volume_size           = 40
    encrypted             = true
    delete_on_termination = true
    tags                  = merge(var.tags, { Name = "klip-${each.key}-root", Node = each.key })
  }
  tags       = merge(var.tags, { Name = "klip-${each.key}", Node = each.key })
  depends_on = [aws_iam_role_policy_attachment.ssm]
  lifecycle {
    # With an explicit primary interface the provider forbids setting this on
    # the instance. The ENI resource owns it; ignore the instance's true default.
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

resource "aws_ebs_volume" "etcd" {
  for_each          = var.nodes
  availability_zone = each.value.availability_zone
  type              = "gp3"
  size              = 20
  encrypted         = true
  tags              = merge(var.tags, { Name = "klip-${each.key}-etcd", Node = each.key, Purpose = "etcd" })
}

resource "aws_volume_attachment" "etcd" {
  for_each                       = var.nodes
  device_name                    = "/dev/sdf"
  volume_id                      = aws_ebs_volume.etcd[each.key].id
  instance_id                    = aws_instance.node[each.key].id
  force_detach                   = false
  stop_instance_before_detaching = true
}
