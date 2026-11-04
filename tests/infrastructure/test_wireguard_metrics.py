"""WireGuard telemetry reports absence explicitly without exposing peer keys or private config."""

import base64
import importlib.util

import pytest
from common import ROOT

spec = importlib.util.spec_from_file_location(
    "klip_wireguard_metrics", ROOT / "observability/agents/wireguard_metrics.py"
)
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)


def registry():
    own = base64.b64encode(b"a" * 32).decode()
    peer = base64.b64encode(b"b" * 32).decode()
    return {"peers": {"cp-01": {"public_key": own}, "platform-01": {"public_key": peer}}}, peer


def test_missing_peer_is_absent_not_a_fabricated_handshake_or_counter():
    source, peer = registry()
    text = metrics.render(source, "cp-01", {}, {}, 1234)
    assert 'klip_wireguard_peer_present{peer="platform-01"} 0' in text
    assert "klip_wireguard_last_handshake_seconds{peer=" not in text
    assert "klip_wireguard_received_bytes_total{peer=" not in text
    assert peer not in text


def test_known_peer_counters_and_unknown_peer_count_do_not_export_keys():
    source, peer = registry()
    unknown = base64.b64encode(b"c" * 32).decode()
    text = metrics.render(
        source, "cp-01", {peer: [1200], unknown: [1201]}, {peer: [100, 200], unknown: [1, 2]}, 1234
    )
    assert 'klip_wireguard_peer_present{peer="platform-01"} 1' in text
    assert 'klip_wireguard_last_handshake_seconds{peer="platform-01"} 1200' in text
    assert 'klip_wireguard_received_bytes_total{peer="platform-01"} 100' in text
    assert 'klip_wireguard_sent_bytes_total{peer="platform-01"} 200' in text
    assert "klip_wireguard_unknown_peers 1" in text
    assert peer not in text and unknown not in text
    assert "PrivateKey" not in text


def test_duplicate_registry_keys_are_not_reported_as_two_valid_peers():
    source, peer = registry()
    source["peers"]["cp-01"]["public_key"] = peer
    with pytest.raises(ValueError, match="duplicate"):
        metrics.render(source, "cp-01", {peer: [1200]}, {peer: [1, 2]}, 1234)


def test_observability_sources_are_frozen_but_python_caches_are_not(monkeypatch, tmp_path):
    import common

    monkeypatch.setattr(common, "ROOT", tmp_path)
    monkeypatch.setattr(common, "TERRAFORM", tmp_path / "infra/terraform")
    directory = tmp_path / "observability/grafana"
    directory.mkdir(parents=True)
    document = directory / "example.json"
    document.write_text('{"synthetic":1}')
    first = common.source_digest()
    cache = directory / "__pycache__"
    cache.mkdir()
    (cache / "local.pyc").write_bytes(b"synthetic-cache")
    assert common.source_digest() == first
    document.write_text('{"synthetic":2}')
    assert common.source_digest() != first
