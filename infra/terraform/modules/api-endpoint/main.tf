resource "aws_lb" "api" {
  name                             = "klip-api"
  internal                         = true
  ip_address_type                  = "ipv4"
  load_balancer_type               = "network"
  security_groups                  = [var.security_group_id]
  enable_cross_zone_load_balancing = true
  enable_deletion_protection       = var.deletion_protection
  dynamic "subnet_mapping" {
    for_each = var.frontends
    content {
      subnet_id            = subnet_mapping.value.subnet_id
      private_ipv4_address = subnet_mapping.value.private_ip
    }
  }
  tags = var.tags
}

resource "aws_lb_target_group" "api" {
  name               = "klip-api"
  vpc_id             = var.vpc_id
  port               = 6443
  protocol           = "TCP"
  target_type        = "ip"
  preserve_client_ip = false
  health_check {
    enabled             = true
    protocol            = "TCP"
    port                = "traffic-port"
    interval            = 10
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }
  tags = var.tags
}

resource "aws_lb_target_group_attachment" "api" {
  for_each         = var.backend_ips
  target_group_arn = aws_lb_target_group.api.arn
  target_id        = each.value
  port             = 6443
}

resource "aws_lb_listener" "api" {
  load_balancer_arn = aws_lb.api.arn
  port              = 6443
  protocol          = "TCP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
  tags = var.tags
}
