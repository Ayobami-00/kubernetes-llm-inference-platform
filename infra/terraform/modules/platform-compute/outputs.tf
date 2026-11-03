output "security_group_id" {
  description = "Worker security group, used by later explicitly scoped storage/gateway rules."
  value       = aws_security_group.platform.id
}
output "nodes" {
  description = "Allocation only; no Kubernetes, storage or application readiness is implied."
  value = { for name, node in var.nodes : name => {
    instance_id       = aws_instance.node[name].id
    availability_zone = node.availability_zone
    private_ip        = node.private_ip
    public_ip         = aws_eip.node[name].public_ip
    elastic_ip_id     = aws_eip.node[name].id
    eni_id            = aws_network_interface.node[name].id
    root_volume_id    = aws_instance.node[name].root_block_device[0].volume_id
    wireguard_ip      = node.wireguard_ip
    labels = {
      "klip.dev/node-role"          = "platform"
      "topology.kubernetes.io/zone" = node.availability_zone
    }
  } }
  depends_on = [aws_eip_association.node]
}
