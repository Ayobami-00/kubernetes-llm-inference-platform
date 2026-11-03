"""Infrastructure-only fixtures; no AWS credentials, network calls or private keys."""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/infrastructure"))

from common import CONFIG  # noqa: E402


@pytest.fixture
def project_config():
    return copy.deepcopy(CONFIG)


@pytest.fixture
def allocation(project_config):
    nodes = {}
    for index, (name, node) in enumerate(project_config["nodes"].items(), 1):
        nodes[name] = {
            "instance_id": f"i-{index:017x}",
            "availability_zone": node["availability_zone"],
            "private_ip": node["private_ip"],
            "public_ip": f"203.0.113.{10 + index}",
            "elastic_ip_id": f"eipalloc-{index:017x}",
            "eni_id": f"eni-{index:017x}",
            "root_volume_id": f"vol-{2 * index:017x}",
            "etcd_volume_id": f"vol-{2 * index + 1:017x}",
            "wireguard_ip": node["wireguard_ip"],
            "ssh_user": "ubuntu",
            "ssh_credential_ref": project_config["ssh_credential_ref"],
        }
    return {
        "schema_version": "foundation-allocation.v1",
        "cluster_id": project_config["cluster_id"],
        "account_id": project_config["account_id"],
        "region": project_config["region"],
        "project": project_config["project"],
        "ami_id": project_config["ami_id"],
        "instance_type": project_config["instance_type"],
        "deletion_protection": True,
        "network": {
            **{
                key: project_config[key]
                for key in (
                    "vpc_cidr",
                    "wireguard_cidr",
                    "pod_cidr",
                    "service_cidr",
                    "worker_wireguard_range",
                )
            },
            "vpc_id": "vpc-00000000000000001",
            "api_routed_cidrs": [
                node["api_subnet_cidr"] for node in project_config["nodes"].values()
            ],
            "cpu_subnet_ids": {name: f"subnet-{index:017x}" for index, name in enumerate(nodes, 1)},
            "api_subnet_ids": {
                name: f"subnet-{index + 3:017x}" for index, name in enumerate(nodes, 1)
            },
            "cpu_security_group_id": "sg-00000000000000001",
            "api_security_group_id": "sg-00000000000000002",
        },
        "api": {
            "endpoint": "https://internal-klip.elb.eu-west-2.amazonaws.com:6443",
            "dns_name": "internal-klip.elb.eu-west-2.amazonaws.com",
            "arn": (
                "arn:aws:elasticloadbalancing:eu-west-2:127811062476:"
                "loadbalancer/net/klip-api/0000000000000001"
            ),
            "target_group_arn": (
                "arn:aws:elasticloadbalancing:eu-west-2:127811062476:"
                "targetgroup/klip-api/0000000000000001"
            ),
            "frontend_ips": ["10.40.16.10", "10.40.17.10", "10.40.18.10"],
        },
        "inference_endpoint": "https://inference.klip-lab.click",
        "nodes": nodes,
    }


@pytest.fixture
def aws_resources(allocation, project_config):
    instances, volumes, interfaces = [], [], []
    for name, node in allocation["nodes"].items():
        instances.append(
            {
                "InstanceId": node["instance_id"],
                "State": {"Name": "running"},
                "PrivateIpAddress": node["private_ip"],
                "PublicIpAddress": node["public_ip"],
                "Placement": {"AvailabilityZone": node["availability_zone"]},
                "ImageId": project_config["ami_id"],
                "InstanceType": project_config["instance_type"],
                "MetadataOptions": {
                    "HttpTokens": "required",
                    "HttpPutResponseHopLimit": 1,
                    "InstanceMetadataTags": "disabled",
                },
                "KeyName": "klip-foundation",
                "IamInstanceProfile": {
                    "Arn": (
                        f"arn:aws:iam::{project_config['account_id']}:instance-profile/"
                        f"{project_config['project']}/klip-control-plane"
                    )
                },
                "Tags": [
                    {"Key": "Project", "Value": project_config["project"]},
                    {"Key": "Node", "Value": name},
                ],
                "BlockDeviceMappings": [
                    {
                        "Ebs": {
                            "VolumeId": node[field],
                            "DeleteOnTermination": field == "root_volume_id",
                        }
                    }
                    for field in ("root_volume_id", "etcd_volume_id")
                ],
            }
        )
        for field, size in (("root_volume_id", 40), ("etcd_volume_id", 20)):
            volumes.append(
                {
                    "VolumeId": node[field],
                    "Encrypted": True,
                    "VolumeType": "gp3",
                    "Size": size,
                    "AvailabilityZone": node["availability_zone"],
                    "Attachments": [{"InstanceId": node["instance_id"], "State": "attached"}],
                }
            )
        interfaces.append(
            {
                "NetworkInterfaceId": node["eni_id"],
                "PrivateIpAddress": node["private_ip"],
                "SourceDestCheck": False,
                "Groups": [{"GroupId": allocation["network"]["cpu_security_group_id"]}],
            }
        )
    return instances, volumes, interfaces
