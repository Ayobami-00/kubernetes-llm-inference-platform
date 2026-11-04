"""Persistent CPU allocation cannot alias control-plane slots or GPU mesh addresses."""

import copy
import json

import pytest
from common import ROOT
from configuration import validate_platform_configuration
from platform_capacity import ensure_capacity


@pytest.fixture
def platform():
    return json.loads((ROOT / "infra/terraform/platform.json").read_text())


def test_platform_slots_are_valid_and_distinct_from_control_planes(project_config, platform):
    validate_platform_configuration(project_config, platform)
    native = {node["private_ip"] for node in project_config["nodes"].values()}
    assert not native.intersection(node["private_ip"] for node in platform["nodes"].values())
    assert {node["wireguard_ip"] for node in platform["nodes"].values()} == {
        "10.200.0.64",
        "10.200.0.65",
        "10.200.0.66",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("foundation_slot", "cp-02"),
        ("availability_zone", "eu-west-2b"),
        ("private_ip", "10.40.0.10"),
        ("private_ip", "10.40.0.2"),
        ("private_ip", "10.40.0.255"),
        ("private_ip", "10.40.1.20"),
        ("wireguard_ip", "10.200.0.11"),
        ("wireguard_ip", "10.200.0.67"),
    ],
)
def test_worker_addressing_cannot_escape_its_slot(project_config, platform, field, value):
    platform["nodes"]["platform-01"][field] = value
    with pytest.raises(ValueError):
        validate_platform_configuration(project_config, platform)


@pytest.mark.parametrize("size", [True, 0, 39, 80.5])
def test_invalid_disk_counts_are_rejected(project_config, platform, size):
    platform["root_volume_gib"] = size
    with pytest.raises(ValueError):
        validate_platform_configuration(project_config, platform)


def test_unknown_fields_and_missing_ha_members_fail_before_planning(project_config, platform):
    for invalid in [
        {**platform, "unreviewed": True},
        {**platform, "schema_version": "different.v1"},
        {**platform, "instance_type": "*"},
        {**platform, "nodes": {"platform-01": platform["nodes"]["platform-01"]}},
    ]:
        with pytest.raises(ValueError):
            validate_platform_configuration(project_config, invalid)
    invalid = copy.deepcopy(platform)
    invalid["nodes"]["platform-01"]["unreviewed"] = True
    with pytest.raises(ValueError):
        validate_platform_configuration(project_config, invalid)


class CapacityMetadata:
    def __init__(self, ips=8, cpus=32):
        self.ips, self.cpus, self.calls = ips, cpus, []

    def call(self, *args):
        self.calls.append(args)
        if args[:2] == ("ec2", "describe-addresses"):
            return {"Addresses": [{}, {}, {}]}
        if args[:2] == ("service-quotas", "get-service-quota"):
            return {"Quota": {"Value": self.ips if args[-1] == "L-0263D0A3" else self.cpus}}
        if args[:2] == ("ec2", "describe-instances"):
            return {
                "Reservations": [
                    {
                        "Instances": [
                            *[{"InstanceType": "m7i.large"} for _ in range(3)],
                            {"InstanceType": "m7i.xlarge", "InstanceLifecycle": "spot"},
                            {"InstanceType": "g6.xlarge"},
                        ]
                    }
                ]
            }
        if args[:2] == ("ec2", "describe-instance-types"):
            return {
                "InstanceTypes": [
                    {"InstanceType": name, "VCpuInfo": {"DefaultVCpus": count}}
                    for name, count in [("m7i.large", 2), ("m7i.xlarge", 4)]
                ]
            }
        raise AssertionError("Unexpected cloud call")


def allocation_changes():
    return {
        "resource_changes": [
            *[
                {
                    "type": "aws_instance",
                    "change": {"actions": ["create"], "after": {"instance_type": "m7i.xlarge"}},
                }
                for _ in range(3)
            ],
            *[{"type": "aws_eip", "change": {"actions": ["create"]}} for _ in range(3)],
        ]
    }


def test_live_capacity_gate_counts_incremental_resources_and_all_regional_usage():
    aws = CapacityMetadata()
    evidence = ensure_capacity(aws, allocation_changes())
    assert evidence == {
        "elastic_ips": {"used": 3, "additional": 3, "quota": 8},
        "standard_vcpus": {"used": 6, "additional": 12, "quota": 32},
    }
    # Spot and accelerator usage have separate quotas and must not consume this one.
    assert not any(
        args[1].startswith(("create", "request", "run", "allocate")) for args in aws.calls
    )


@pytest.mark.parametrize(
    "ips,cpus,reason", [(5, 32, "6 Elastic IPs"), (8, 16, "18 Standard vCPUs")]
)
def test_insufficient_quota_stops_before_provisioning(ips, cpus, reason):
    with pytest.raises(ValueError, match=reason):
        ensure_capacity(CapacityMetadata(ips, cpus), allocation_changes())


@pytest.mark.parametrize("limit", [True, float("nan"), float("inf"), -1])
def test_missing_or_invalid_capacity_is_not_treated_as_unlimited(limit):
    with pytest.raises(ValueError, match="verified regional"):
        ensure_capacity(CapacityMetadata(ips=limit), allocation_changes())


def test_no_change_plan_needs_no_additional_quota_or_network_calls():
    aws = CapacityMetadata()
    assert ensure_capacity(aws, {"resource_changes": []}) == {}
    assert not aws.calls
