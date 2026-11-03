import pytest
from configuration import validate_configuration, validate_origins


def test_agreed_configuration_is_disjoint(project_config):
    validate_configuration(project_config, ["172.31.0.0/16", "192.168.1.0/24"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("wireguard_cidr", "10.40.0.0/24"),
        ("pod_cidr", "10.96.0.0/16"),
        ("service_cidr", "10.244.0.0/16"),
        ("vpc_cidr", "10.40.0.1/16"),
    ],
)
def test_reject_overlapping_or_noncanonical_ranges(project_config, field, value):
    project_config[field] = value
    with pytest.raises(ValueError):
        validate_configuration(project_config)


@pytest.mark.parametrize(
    "field,value",
    [
        ("private_ip", "10.40.1.10"),
        ("private_ip", "10.40.0.2"),
        ("wireguard_ip", "10.200.0.12"),
        ("availability_zone", "eu-west-2b"),
        ("api_subnet_cidr", "10.40.1.0/24"),
    ],
)
def test_reject_invalid_host_assignments(project_config, field, value):
    project_config["nodes"]["cp-01"][field] = value
    with pytest.raises(ValueError):
        validate_configuration(project_config)


def test_reject_future_provider_overlap(project_config):
    with pytest.raises(ValueError, match="overlap"):
        validate_configuration(project_config, ["10.40.0.0/16"])


def test_worker_pool_cannot_reuse_cpu_addresses(project_config):
    project_config["worker_wireguard_range"] = ["10.200.0.10", "10.200.0.64"]
    with pytest.raises(ValueError, match="Worker range"):
        validate_configuration(project_config)


def test_empty_origins_and_mock_only_public_origin():
    validate_origins({})
    validate_origins({"test-only": {"ipv4": "8.8.8.8"}})


@pytest.mark.parametrize(
    "ip", ["10.0.0.1", "127.0.0.1", "192.0.2.5", "169.254.1.1", "224.0.0.1", "::1"]
)
def test_reject_nonpublic_origins(ip):
    with pytest.raises(ValueError):
        validate_origins({"test-only": {"ipv4": ip}})


@pytest.mark.parametrize("weight", [0, -1, 256, 1.5, True])
def test_origin_weights_are_explicit_positive_integers(weight):
    with pytest.raises(ValueError, match="weights"):
        validate_origins({"test-only": {"ipv4": "8.8.8.8", "weight": weight}})


def test_duplicate_origin_addresses_are_rejected():
    with pytest.raises(ValueError, match="Duplicate"):
        validate_origins({"one": {"ipv4": "8.8.8.8"}, "two": {"ipv4": "8.8.8.8"}})


def test_swapped_but_distinct_azs_are_rejected(project_config):
    project_config["nodes"]["cp-01"]["availability_zone"] = "eu-west-2b"
    project_config["nodes"]["cp-02"]["availability_zone"] = "eu-west-2a"
    with pytest.raises(ValueError, match="mapping"):
        validate_configuration(project_config)


def test_worker_range_cannot_include_broadcast(project_config):
    project_config["worker_wireguard_range"][1] = "10.200.0.255"
    with pytest.raises(ValueError, match="Worker range"):
        validate_configuration(project_config)


@pytest.mark.parametrize(
    "origins",
    [
        [],
        {"one": {}},
        {"one": {"ipv4": 134744072}},
        {"one": {"ipv4": "8.8.8.8", "private_key": "bad"}},
    ],
)
def test_malformed_origins_fail_clearly(origins):
    with pytest.raises(ValueError, match="map|requires"):
        validate_origins(origins)
