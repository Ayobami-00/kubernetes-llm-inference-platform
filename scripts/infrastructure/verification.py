"""Read-only AWS acceptance checks, separate from Terraform's allocation state."""

from __future__ import annotations

import json
import socket
from ipaddress import IPv4Network

from common import CONFIG, Aws, state_home, utc_now, write_private
from smoke import ssm_online, wait_until


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def expected_rules(network: dict) -> set[tuple]:
    cpu, api = network["cpu_security_group_id"], network["api_security_group_id"]
    resolver = f"{IPv4Network(network['vpc_cidr']).network_address + 2}/32"
    rules = {
        (cpu, False, "udp", 51820, 51820, "0.0.0.0/0"),
        (cpu, False, "tcp", 6443, 6443, api),
        (api, False, "tcp", 6443, 6443, cpu),
        (cpu, True, "tcp", 6443, 6443, api),
        (api, True, "tcp", 6443, 6443, cpu),
    }
    for protocol, port in (("tcp", 80), ("tcp", 443), ("udp", 51820)):
        rules.add((cpu, True, protocol, port, port, "0.0.0.0/0"))
    for protocol in ("tcp", "udp"):
        rules.add((cpu, True, protocol, 53, 53, resolver))
    for group in (cpu, api):
        for egress in (False, True):
            rules.add((group, egress, "icmp", 3, 4, "0.0.0.0/0"))
    return rules


def verify_security_rules(network: dict, rules: list[dict]) -> None:
    actual = {
        (
            item["GroupId"],
            item["IsEgress"],
            item["IpProtocol"],
            item.get("FromPort"),
            item.get("ToPort"),
            item.get("CidrIpv4")
            or item.get("ReferencedGroupInfo", {}).get("GroupId")
            or item.get("CidrIpv6"),
        )
        for item in rules
    }
    if actual != expected_rules(network):
        raise ValueError(
            "Security groups differ from the reviewed rules: "
            f"missing={expected_rules(network) - actual}; extra={actual - expected_rules(network)}"
        )


def verify_instances(
    manifest: dict, instances: list[dict], volumes: list[dict], interfaces: list[dict]
) -> None:
    indexed = {item["InstanceId"]: item for item in instances}
    disks = {item["VolumeId"]: item for item in volumes}
    enis = {item["NetworkInterfaceId"]: item for item in interfaces}
    for name, node in manifest["nodes"].items():
        instance = indexed.get(node["instance_id"])
        require(instance is not None, f"Missing instance {name}")
        require(instance["State"]["Name"] == "running", f"{name} is not running")
        require(
            instance["ImageId"] == CONFIG["ami_id"]
            and instance["InstanceType"] == CONFIG["instance_type"],
            "Unexpected image or instance size",
        )
        require(
            instance["Placement"]["AvailabilityZone"] == CONFIG["nodes"][name]["availability_zone"],
            "Incorrect replica placement",
        )
        require(
            instance["PrivateIpAddress"] == node["private_ip"]
            and instance.get("PublicIpAddress") == node["public_ip"],
            "Address allocation drift",
        )
        metadata = instance["MetadataOptions"]
        require(
            metadata["HttpTokens"] == "required"
            and metadata["HttpPutResponseHopLimit"] == 1
            and metadata["InstanceMetadataTags"] == "disabled",
            "Unsafe metadata settings",
        )
        require(instance.get("KeyName") == "klip-foundation", "Unexpected SSH public key")
        require(
            instance.get("IamInstanceProfile", {}).get("Arn")
            == (
                f"arn:aws:iam::{CONFIG['account_id']}:instance-profile/"
                f"{CONFIG['project']}/klip-control-plane"
            ),
            "Unexpected instance profile",
        )
        tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
        require(
            tags.get("Project") == CONFIG["project"] and tags.get("Node") == name,
            "Missing instance ownership tags",
        )
        eni = enis.get(node["eni_id"])
        require(
            eni is not None
            and eni["PrivateIpAddress"] == node["private_ip"]
            and not eni["SourceDestCheck"],
            "Incorrect primary ENI forwarding/address settings",
        )
        require(
            {group["GroupId"] for group in eni["Groups"]}
            == {manifest["network"]["cpu_security_group_id"]},
            "Unexpected host security group",
        )
        mapping = {
            item["Ebs"]["VolumeId"]: item["Ebs"]
            for item in instance["BlockDeviceMappings"]
            if "Ebs" in item
        }
        for field, size in (("root_volume_id", 40), ("etcd_volume_id", 20)):
            disk = disks.get(node[field])
            require(
                disk is not None
                and disk["Encrypted"]
                and disk["VolumeType"] == "gp3"
                and disk["Size"] == size,
                "Disk encryption, type or size differs from the plan",
            )
            require(
                disk["AvailabilityZone"] == instance["Placement"]["AvailabilityZone"],
                "Disk AZ mismatch",
            )
            require(
                any(
                    item["InstanceId"] == node["instance_id"] and item["State"] == "attached"
                    for item in disk["Attachments"]
                ),
                "Disk is not attached to its allocated host",
            )
            require(
                node[field] in mapping
                and mapping[node[field]]["DeleteOnTermination"] == (field == "root_volume_id"),
                "Incorrect disk deletion semantics",
            )


def verify_aws(aws: Aws, manifest: dict) -> list[dict]:
    from inventory import FoundationAllocation

    FoundationAllocation.model_validate(manifest)
    ids = [node["instance_id"] for node in manifest["nodes"].values()]

    def status_ok():
        values = aws.call(
            "ec2", "describe-instance-status", "--include-all-instances", "--instance-ids", *ids
        )["InstanceStatuses"]
        return len(values) == 3 and all(
            item["InstanceStatus"]["Status"] == "ok" and item["SystemStatus"]["Status"] == "ok"
            for item in values
        )

    wait_until(status_ok, "all EC2 status checks", timeout=600)
    for instance_id in ids:
        wait_until(
            lambda instance_id=instance_id: ssm_online(aws, instance_id),
            "SSM host registration",
            timeout=600,
        )
    reservations = aws.call("ec2", "describe-instances", "--instance-ids", *ids)["Reservations"]
    instances = [item for reservation in reservations for item in reservation["Instances"]]
    disks = [
        node[field]
        for node in manifest["nodes"].values()
        for field in ("root_volume_id", "etcd_volume_id")
    ]
    volumes = aws.call("ec2", "describe-volumes", "--volume-ids", *disks)["Volumes"]
    interfaces = aws.call(
        "ec2",
        "describe-network-interfaces",
        "--network-interface-ids",
        *[node["eni_id"] for node in manifest["nodes"].values()],
    )["NetworkInterfaces"]
    verify_instances(manifest, instances, volumes, interfaces)
    for instance_id in ids:
        protection = aws.call(
            "ec2",
            "describe-instance-attribute",
            "--instance-id",
            instance_id,
            "--attribute",
            "disableApiTermination",
        )["DisableApiTermination"]["Value"]
        require(
            protection == manifest["deletion_protection"], "Instance termination protection drift"
        )
    network = manifest["network"]
    rules = aws.call(
        "ec2",
        "describe-security-group-rules",
        "--filters",
        json.dumps(
            [
                {
                    "Name": "group-id",
                    "Values": [network["cpu_security_group_id"], network["api_security_group_id"]],
                }
            ]
        ),
    )["SecurityGroupRules"]
    verify_security_rules(network, rules)
    for kind in ("cpu", "api"):
        subnet_ids = list(network[f"{kind}_subnet_ids"].values())
        tables = aws.call(
            "ec2",
            "describe-route-tables",
            "--filters",
            json.dumps([{"Name": "association.subnet-id", "Values": subnet_ids}]),
        )["RouteTables"]
        associated = {item.get("SubnetId") for table in tables for item in table["Associations"]}
        require(
            set(subnet_ids).issubset(associated), "Missing explicit subnet route-table associations"
        )
        for table in tables:
            defaults = [
                route
                for route in table["Routes"]
                if route.get("DestinationCidrBlock") == "0.0.0.0/0"
                or route.get("DestinationIpv6CidrBlock") == "::/0"
            ]
            if kind == "cpu":
                require(
                    len(defaults) == 1
                    and defaults[0].get("GatewayId", "").startswith("igw-")
                    and defaults[0]["State"] == "active",
                    "CPU subnets require direct IGW egress",
                )
            else:
                require(
                    not defaults, "Private API subnets must not have internet/NAT default routes"
                )
    lb = aws.call(
        "elbv2", "describe-load-balancers", "--load-balancer-arns", manifest["api"]["arn"]
    )["LoadBalancers"][0]
    require(
        lb["Scheme"] == "internal"
        and lb["Type"] == "network"
        and lb["IpAddressType"] == "ipv4"
        and lb["State"]["Code"] == "active",
        "API NLB is not active/private/IPv4",
    )
    require(
        set(lb["SecurityGroups"]) == {network["api_security_group_id"]}, "NLB security-group drift"
    )
    attributes = {
        item["Key"]: item["Value"]
        for item in aws.call(
            "elbv2",
            "describe-load-balancer-attributes",
            "--load-balancer-arn",
            manifest["api"]["arn"],
        )["Attributes"]
    }
    require(
        attributes["load_balancing.cross_zone.enabled"] == "true"
        and attributes["deletion_protection.enabled"]
        == str(manifest["deletion_protection"]).lower(),
        "NLB balancing/protection drift",
    )
    frontends = {
        address["PrivateIPv4Address"]
        for zone in lb["AvailabilityZones"]
        for address in zone.get("LoadBalancerAddresses", [])
        if "PrivateIPv4Address" in address
    }
    require(frontends == set(manifest["api"]["frontend_ips"]), "NLB static frontend IP drift")
    group = aws.call(
        "elbv2",
        "describe-target-groups",
        "--target-group-arns",
        manifest["api"]["target_group_arn"],
    )["TargetGroups"][0]
    require(
        group["Protocol"] == "TCP"
        and group["Port"] == 6443
        and group["TargetType"] == "ip"
        and group["HealthCheckProtocol"] == "TCP"
        and group["HealthCheckIntervalSeconds"] == 10
        and group["HealthyThresholdCount"] == 2
        and group["UnhealthyThresholdCount"] == 2,
        "API target-group configuration drift",
    )
    group_attributes = {
        item["Key"]: item["Value"]
        for item in aws.call(
            "elbv2",
            "describe-target-group-attributes",
            "--target-group-arn",
            manifest["api"]["target_group_arn"],
        )["Attributes"]
    }
    require(
        group_attributes["preserve_client_ip.enabled"] == "false",
        "Client-IP preservation would break target-to-NLB hairpin access",
    )
    health = aws.call(
        "elbv2", "describe-target-health", "--target-group-arn", manifest["api"]["target_group_arn"]
    )["TargetHealthDescriptions"]
    require(
        {item["Target"]["Id"] for item in health}
        == {node["private_ip"] for node in manifest["nodes"].values()},
        "API target identities differ from CPU native IPs",
    )
    listeners = aws.call(
        "elbv2", "describe-listeners", "--load-balancer-arn", manifest["api"]["arn"]
    )["Listeners"]
    require(
        len(listeners) == 1
        and listeners[0]["Protocol"] == "TCP"
        and listeners[0]["Port"] == 6443
        and listeners[0]["DefaultActions"][0]["TargetGroupArn"] == group["TargetGroupArn"],
        "Unexpected API listener or routing",
    )
    # Negative public exposure checks. A connection, even immediately closed, is failure.
    for node in manifest["nodes"].values():
        for port in (22, 6443):
            try:
                with socket.create_connection((node["public_ip"], port), timeout=2):
                    raise ValueError(
                        f"Unexpected public TCP exposure on {node['public_ip']}:{port}"
                    )
            except (TimeoutError, ConnectionRefusedError, OSError):
                pass
    write_private(
        state_home() / "reports/aws-verification.json",
        {
            "verified_at": utc_now(),
            "passed": True,
            "scope": "AWS resources and access controls",
            "target_health": health,
            "note": (
                "Unhealthy targets are expected outside temporary smoke tests "
                "until Kubernetes bootstrap"
            ),
        },
    )
    return instances
