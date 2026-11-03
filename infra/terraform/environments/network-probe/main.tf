locals {
  config = jsondecode(file("${path.module}/../../project.json"))
  tags = {
    Project     = local.config.project, Cluster = local.config.cluster_id,
    Environment = local.config.environment, ManagedBy = "terraform-network-probe", Node = "network-probe"
  }
}

variable "ssh_public_key" {
  description = "Public SSH key only; private material remains with the operator."
  type        = string
  validation {
    condition     = can(regex("^ssh-ed25519 [A-Za-z0-9+/]+=*( .*)?$", var.ssh_public_key))
    error_message = "Supply an Ed25519 public key."
  }
}

resource "aws_vpc" "probe" {
  cidr_block           = "10.41.0.0/24"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = local.tags
}
resource "aws_subnet" "probe" {
  vpc_id            = aws_vpc.probe.id
  cidr_block        = "10.41.0.0/24"
  availability_zone = "eu-west-2a"
  tags              = local.tags
}
resource "aws_internet_gateway" "probe" {
  vpc_id = aws_vpc.probe.id
  tags   = local.tags
}
resource "aws_route_table" "probe" {
  vpc_id = aws_vpc.probe.id
  tags   = local.tags
}
resource "aws_route" "egress" {
  route_table_id         = aws_route_table.probe.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.probe.id
}
resource "aws_route_table_association" "probe" {
  subnet_id      = aws_subnet.probe.id
  route_table_id = aws_route_table.probe.id
}
resource "aws_security_group" "probe" {
  name        = "klip-network-probe"
  description = "Temporary independent WireGuard test host; no public SSH or API"
  vpc_id      = aws_vpc.probe.id
  tags        = local.tags
}
resource "aws_vpc_security_group_ingress_rule" "wireguard" {
  security_group_id = aws_security_group.probe.id
  ip_protocol       = "udp"
  from_port         = 51820
  to_port           = 51820
  cidr_ipv4         = "0.0.0.0/0"
  tags              = local.tags
}
resource "aws_vpc_security_group_ingress_rule" "pmtu" {
  security_group_id = aws_security_group.probe.id
  ip_protocol       = "icmp"
  from_port         = 3
  to_port           = 4
  cidr_ipv4         = "0.0.0.0/0"
  tags              = local.tags
}
resource "aws_vpc_security_group_egress_rule" "packages" {
  for_each          = toset(["80", "443"])
  security_group_id = aws_security_group.probe.id
  ip_protocol       = "tcp"
  from_port         = tonumber(each.key)
  to_port           = tonumber(each.key)
  cidr_ipv4         = "0.0.0.0/0"
  tags              = local.tags
}
resource "aws_vpc_security_group_egress_rule" "wireguard" {
  security_group_id = aws_security_group.probe.id
  ip_protocol       = "udp"
  from_port         = 51820
  to_port           = 51820
  cidr_ipv4         = "0.0.0.0/0"
  tags              = local.tags
}
resource "aws_iam_role" "probe" {
  name                 = "klip-network-probe"
  path                 = "/${local.config.project}/"
  permissions_boundary = "arn:aws:iam::${local.config.account_id}:policy/${local.config.project}/kubernetes-llm-control-plane-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17", Statement = [{
      Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
  tags = local.tags
}
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.probe.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_instance_profile" "probe" {
  name = "klip-network-probe"
  path = "/${local.config.project}/"
  role = aws_iam_role.probe.name
  tags = local.tags
}
resource "aws_key_pair" "probe" {
  key_name   = "klip-network-probe"
  public_key = var.ssh_public_key
  tags       = local.tags
}
resource "aws_instance" "probe" {
  ami           = local.config.ami_id
  instance_type = "t3.medium"
  primary_network_interface {
    network_interface_id = aws_network_interface.probe.id
  }
  key_name                             = aws_key_pair.probe.key_name
  iam_instance_profile                 = aws_iam_instance_profile.probe.name
  instance_initiated_shutdown_behavior = "terminate"
  # The only cloud-init action is an expiring acceptance-host safety timer.
  user_data = <<-CLOUD
    #cloud-config
    runcmd:
      - |
        deadline=$(date -u -d '+4 hours' '+%Y-%m-%d %H:%M:%S UTC')
        printf '[Unit]\nDescription=Expire temporary probe\n[Service]\nType=oneshot\nExecStart=/sbin/shutdown -h now\n' > /etc/systemd/system/klip-probe-expiry.service
        printf '[Unit]\nDescription=Fixed probe expiry\n[Timer]\nOnCalendar=%s\nPersistent=true\n[Install]\nWantedBy=timers.target\n' "$deadline" > /etc/systemd/system/klip-probe-expiry.timer
        systemctl daemon-reload
        systemctl enable --now klip-probe-expiry.timer
  CLOUD
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }
  credit_specification {
    cpu_credits = "standard"
  }
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 20
    encrypted             = true
    delete_on_termination = true
    tags                  = local.tags
  }
  tags       = local.tags
  depends_on = [aws_route.egress, aws_iam_role_policy_attachment.ssm]
}
resource "aws_network_interface" "probe" {
  subnet_id       = aws_subnet.probe.id
  private_ips     = ["10.41.0.10"]
  security_groups = [aws_security_group.probe.id]
  tags            = local.tags
}
resource "aws_eip" "probe" {
  domain            = "vpc"
  network_interface = aws_network_interface.probe.id
  tags              = local.tags
  depends_on        = [aws_instance.probe]
}
output "probe" {
  description = "Separate-network acceptance allocation; never a shared GPU inventory entry."
  value = {
    instance_id       = aws_instance.probe.id, public_ip = aws_eip.probe.public_ip,
    allocation_id     = aws_eip.probe.id,
    availability_zone = aws_instance.probe.availability_zone,
    private_ip        = aws_instance.probe.private_ip, wireguard_ip = "10.200.0.2",
    vpc_id            = aws_vpc.probe.id, cidr = aws_vpc.probe.cidr_block
  }
}
