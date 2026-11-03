output "nodes" {
  description = "Allocated host resources; these are not readiness observations."
  value = { for key, node in var.nodes : key => {
    instance_id       = aws_instance.node[key].id
    availability_zone = node.availability_zone
    private_ip        = node.private_ip
    public_ip         = aws_eip.node[key].public_ip
    elastic_ip_id     = aws_eip.node[key].id
    eni_id            = aws_network_interface.node[key].id
    etcd_volume_id    = aws_ebs_volume.etcd[key].id
    root_volume_id    = aws_instance.node[key].root_block_device[0].volume_id
    wireguard_ip      = node.wireguard_ip
  } }
  depends_on = [aws_eip_association.node, aws_volume_attachment.etcd]
}
