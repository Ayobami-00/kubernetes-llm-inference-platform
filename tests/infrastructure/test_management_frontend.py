"""Frontend publication needs exact live ownership and a fresh HTTPS proof."""

import copy
import json
from unittest.mock import Mock

import management_frontend as frontend
import pytest
from common import CONFIG


def allocation():
    return {
        **{k: CONFIG[k] for k in ("project", "account_id", "region", "cluster_id")},
        "hostname": "management." + CONFIG["domain"],
        "load_balancer_arn": "lb",
        "target_group_arn": "tg",
        "dns_name": "klip.eu-west-2.elb.amazonaws.com",
        "node_port": 30443,
        "nodes": {"one": "i-1", "two": "i-2", "three": "i-3"},
    }


def test_publication_defaults_off_and_refuses_stale_allocation(tmp_path, monkeypatch):
    monkeypatch.setattr(frontend, "folder", lambda: tmp_path)
    value = allocation()
    monkeypatch.setattr(frontend, "manifest", lambda: value)
    probe = Mock(return_value={"verified": True})
    monkeypatch.setattr(frontend, "verify", probe)
    assert not frontend.publish_requested()
    with pytest.raises(ValueError, match="intent"):
        frontend.validate_activation()
    frontend.activate(published=True)
    assert frontend.publish_requested()
    frontend.validate_activation()
    assert probe.call_count == 2  # apply/plan validation never relies on the saved proof
    value["load_balancer_arn"] = "replacement"
    with pytest.raises(ValueError, match="different allocation"):
        frontend.validate_activation()
    frontend.activate(published=False)
    assert not frontend.publish_requested() and probe.call_count == 2
    (tmp_path / "activation.json").write_text(json.dumps({"published": "false"}))
    with pytest.raises(ValueError, match="malformed"):
        frontend.publish_requested()


@pytest.mark.parametrize("fault", [None, "missing", "unhealthy", "wrong_port", "extra", "tags"])
def test_all_three_exact_targets_and_live_ownership_are_required(fault):
    value = allocation()
    targets = [
        {"Target": {"Id": i, "Port": 30443}, "TargetHealth": {"State": "healthy"}}
        for i in value["nodes"].values()
    ]
    tags = {
        "Project": CONFIG["project"],
        "Cluster": CONFIG["cluster_id"],
        "ManagedBy": "terraform-platform-frontend",
    }
    if fault == "missing":
        targets.pop()
    if fault == "unhealthy":
        targets[0]["TargetHealth"]["State"] = "unhealthy"
    if fault == "wrong_port":
        targets[0]["Target"]["Port"] = 8000
    if fault == "extra":
        targets.append(copy.deepcopy(targets[0]))
    if fault == "tags":
        tags["Project"] = "unrelated"
    aws = Mock()
    aws.call.side_effect = [
        {
            "LoadBalancers": [
                {
                    "State": {"Code": "active"},
                    "DNSName": value["dns_name"],
                    "Scheme": "internet-facing",
                }
            ]
        },
        {"TagDescriptions": [{"Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}]},
        {"TargetHealthDescriptions": targets},
    ]
    if fault:
        with pytest.raises(ValueError):
            frontend.check_targets(value, aws)
    else:
        assert len(frontend.check_targets(value, aws)["targets"]) == 3


def test_frontend_refuses_redirects_and_unverified_public_certificates():
    import ssl

    with pytest.raises(ValueError, match="redirect"):
        frontend.FrontendConnection("unrelated.example", connect_host="lb.example")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = frontend.FrontendConnection(
        "management." + CONFIG["domain"], connect_host="lb.example", context=context
    )
    with pytest.raises(ValueError, match="verification"):
        connection.connect()


@pytest.mark.parametrize(
    "peer,forwarded,expected",
    [
        ("10.244.1.5", "203.0.113.9", "203.0.113.9"),
        ("198.51.100.9", "203.0.113.9", "198.51.100.9"),
        ("10.244.1.5", "198.51.100.1, 203.0.113.9", "203.0.113.9"),
    ],
)
def test_actual_pinned_api_middleware_trusts_only_the_configured_proxy_network(
    peer, forwarded, expected
):
    import asyncio

    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    observed = []

    async def app(scope, receive, send):
        observed.append(scope["client"][0])

    middleware = ProxyHeadersMiddleware(app, trusted_hosts="10.244.0.0/16")
    scope = {
        "type": "http",
        "client": (peer, 12345),
        "scheme": "http",
        "headers": [(b"x-forwarded-for", forwarded.encode())],
    }
    asyncio.run(middleware(scope, None, None))
    assert observed == [expected]
