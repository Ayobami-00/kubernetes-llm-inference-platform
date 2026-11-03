"""Cross-node metrics exceptions cannot open other peers, ports or firewall drift."""

import pytest
from cluster_config import firewall, platform_metrics_sources
from telemetry_firewall import reviewed_rules


def config():
    return {
        "role": "worker",
        "provider": "aws",
        "api_frontend_ips": ["10.40.16.10"],
        "wireguard_cidr": "10.200.0.0/24",
        "pod_cidr": "10.244.0.0/16",
        "vpc_cidr": "10.40.0.0/16",
        "api_routed_cidrs": ["10.40.16.0/24"],
    }


def test_review_adds_only_three_wireguard_peer_rules_for_authenticated_kubelet():
    value = config()
    before = firewall(value)
    after = reviewed_rules(value, before)
    added = set(after.splitlines()) - set(before.splitlines())
    assert added == {
        f"-A KLIP-HOST -i wg0 -s {source} -p tcp --dport 10250 -j ACCEPT"
        for source in platform_metrics_sources()
    }
    assert not set(before.splitlines()) - set(after.splitlines())
    assert reviewed_rules(value, after) == after


@pytest.mark.parametrize("source", ["0.0.0.0/0", "10.200.0.99/32", "10.200.0.0/24"])
def test_metrics_sources_cannot_expand_beyond_reserved_platform_hosts(source):
    with pytest.raises(ValueError, match="reserved CPU workers"):
        firewall({**config(), "platform_metrics_sources": [source]})


def test_drift_is_not_overwritten_by_the_metrics_rollout():
    value = config()
    before = firewall(value).replace("-A KLIP-HOST -j DROP", "-A KLIP-HOST -j ACCEPT")
    with pytest.raises(ValueError, match="drifted"):
        reviewed_rules(value, before)
