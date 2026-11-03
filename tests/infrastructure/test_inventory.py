import copy
from datetime import UTC, datetime, timedelta

import pytest
from inventory import (
    FoundationAllocation,
    FoundationObservation,
    ansible_inventory,
    build_observation,
    shared_inventory,
)
from pydantic import ValidationError


def observed(allocation, aws_resources):
    instances, _, _ = aws_resources
    return build_observation(
        allocation, instances, {item["InstanceId"]: True for item in instances}
    )


def test_observed_hosts_are_not_claimed_as_cluster_ready(allocation, aws_resources):
    result = observed(allocation, aws_resources)
    assert all(node.readiness.host for node in result.nodes)
    assert all(
        not node.readiness.wireguard and not node.readiness.kubernetes and not node.readiness.cilium
        for node in result.nodes
    )
    assert all(node.kubernetes is None and not node.readiness.gpu_runtime for node in result.nodes)


def test_failed_host_access_stays_unready(allocation, aws_resources):
    result = build_observation(allocation, aws_resources[0], {})
    assert not any(node.readiness.host for node in result.nodes)


def test_allocation_does_not_create_observations_for_missing_hosts(allocation, aws_resources):
    with pytest.raises(ValueError, match="missing"):
        build_observation(allocation, aws_resources[0][:-1], {})


@pytest.mark.parametrize(
    "field,value", [("private_ip", "10.0.0.5"), ("public_ip", "203.0.113.100")]
)
def test_live_address_drift_is_rejected(allocation, aws_resources, field, value):
    allocation["nodes"]["cp-01"][field] = value
    with pytest.raises(ValueError, match="addresses"):
        observed(allocation, aws_resources)


def test_private_material_cannot_enter_allocation_or_observation(allocation, aws_resources):
    allocation["nodes"]["cp-01"]["private_key"] = "not-a-real-key"
    with pytest.raises(ValidationError, match="Extra inputs"):
        observed(allocation, aws_resources)


def test_duplicate_allocations_are_rejected(allocation):
    allocation["nodes"]["cp-02"]["instance_id"] = allocation["nodes"]["cp-01"]["instance_id"]
    with pytest.raises(ValueError, match="Duplicate"):
        FoundationAllocation.model_validate(allocation)


def test_full_export_refuses_to_invent_a_ca(allocation, aws_resources):
    result = observed(allocation, aws_resources)
    with pytest.raises(ValueError, match="real verified CA"):
        shared_inventory(result, None, None)


def test_full_export_reuses_shared_models_after_verified_bootstrap(allocation, aws_resources):
    result = observed(allocation, aws_resources)
    nodes = [node.model_dump(mode="json") for node in result.nodes]
    for index, node in enumerate(nodes):
        node["readiness"].update(wireguard=True, kubernetes=True, cilium=True)
        node["kubernetes"] = {"name": f"klip-{node['id']}", "uid": f"verified-node-{index}"}
    inventory = shared_inventory(result, "file:///verified/public/ca.crt", nodes)
    assert len(inventory.nodes) == 3
    assert inventory.cluster.ca_bundle_ref == "file:///verified/public/ca.crt"
    assert not inventory.fixture


def test_bootstrap_observations_cannot_change_provider_identity(allocation, aws_resources):
    result = observed(allocation, aws_resources)
    nodes = [node.model_dump(mode="json") for node in result.nodes]
    nodes[0]["provider_instance_id"] = "i-foreign"
    with pytest.raises(ValueError, match="identity"):
        shared_inventory(result, "file:///verified/public/ca.crt", nodes)


def test_bootstrap_cannot_claim_kubernetes_without_node_identity(allocation, aws_resources):
    result = observed(allocation, aws_resources)
    value = result.model_dump(mode="json")
    value["nodes"][0]["readiness"].update(wireguard=True, kubernetes=True)
    with pytest.raises(ValueError, match="real node identity"):
        FoundationObservation.model_validate(value)


def test_future_observations_are_rejected(allocation, aws_resources):
    result = observed(allocation, aws_resources).model_dump(mode="json")
    result["nodes"][0]["observed_at"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="newer"):
        FoundationObservation.model_validate(result)


def test_ansible_inventory_uses_ssm_and_local_credential_reference(
    allocation, aws_resources, tmp_path
):
    value = ansible_inventory(observed(allocation, aws_resources), tmp_path / "private-key")
    hosts = value["all"]["children"]["control_plane"]["hosts"]
    assert set(hosts) == {"cp-01", "cp-02", "cp-03"}
    for host in hosts.values():
        assert host["ansible_host"].startswith("i-")
        assert "AWS-StartSSHSession" in host["ansible_ssh_common_args"]
        assert "StrictHostKeyChecking=no" not in host["ansible_ssh_common_args"]
    assert "private_key" not in copy.deepcopy(allocation)["nodes"]["cp-01"]
