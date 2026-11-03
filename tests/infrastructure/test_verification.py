import pytest
from verification import expected_rules, verify_instances, verify_security_rules


def test_expected_aws_shape_passes(allocation, aws_resources):
    verify_instances(allocation, *aws_resources)


@pytest.mark.parametrize(
    "case", ["unencrypted", "wrong_size", "unsafe_imds", "wrong_az", "delete_etcd", "wrong_group"]
)
def test_material_cloud_drift_fails_acceptance(allocation, aws_resources, case):
    instances, volumes, interfaces = aws_resources
    if case == "unencrypted":
        volumes[0]["Encrypted"] = False
    elif case == "wrong_size":
        volumes[0]["Size"] = 8
    elif case == "unsafe_imds":
        instances[0]["MetadataOptions"]["HttpTokens"] = "optional"
    elif case == "wrong_az":
        instances[0]["Placement"]["AvailabilityZone"] = "eu-west-2d"
    elif case == "delete_etcd":
        instances[0]["BlockDeviceMappings"][1]["Ebs"]["DeleteOnTermination"] = True
    else:
        interfaces[0]["Groups"] = [{"GroupId": "sg-foreign"}]
    with pytest.raises(ValueError):
        verify_instances(allocation, instances, volumes, interfaces)


def rules_from_expected(network):
    result = []
    for group, egress, protocol, start, end, peer in expected_rules(network):
        value = {
            "GroupId": group,
            "IsEgress": egress,
            "IpProtocol": protocol,
            "FromPort": start,
            "ToPort": end,
        }
        if peer.startswith("sg-"):
            value["ReferencedGroupInfo"] = {"GroupId": peer}
        else:
            value["CidrIpv4"] = peer
        result.append(value)
    return result


def test_public_ssh_is_never_accepted_as_extra_rule(allocation):
    network = allocation["network"]
    rules = rules_from_expected(network)
    verify_security_rules(network, rules)
    rules.append(
        {
            "GroupId": network["cpu_security_group_id"],
            "IsEgress": False,
            "IpProtocol": "tcp",
            "FromPort": 22,
            "ToPort": 22,
            "CidrIpv4": "0.0.0.0/0",
        }
    )
    with pytest.raises(ValueError, match="Security groups differ"):
        verify_security_rules(network, rules)


def test_missing_egress_rule_is_not_a_pass(allocation):
    rules = rules_from_expected(allocation["network"])
    with pytest.raises(ValueError, match="missing"):
        verify_security_rules(allocation["network"], rules[:-1])
