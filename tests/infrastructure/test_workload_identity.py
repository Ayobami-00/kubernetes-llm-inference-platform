"""Fail closed before publishing keys or trusting workload JWTs."""

import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import cluster
import pytest
import workload_identity as identity
from common import CONFIG, ROOT, write_private
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture(scope="module")
def public_pem():
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


@pytest.fixture
def jwks(public_pem):
    return {"keys": [identity.public_jwk(public_pem)]}


def test_public_key_matches_its_id_and_private_fields_never_publish(jwks):
    assert identity.validate_jwks(jwks) == jwks
    for field, value in (
        ("d", "private"),
        ("kid", "forged-id"),
        ("alg", "HS256"),
        ("use", "enc"),
        ("n", "invalid!"),
        ("e", "Aw"),
    ):
        invalid = copy.deepcopy(jwks)
        invalid["keys"][0][field] = value
        with pytest.raises(ValueError):
            identity.validate_jwks(invalid)
    with pytest.raises(ValueError, match="duplicate"):
        identity.validate_jwks({"keys": jwks["keys"] * 2})


def test_private_or_weak_key_material_is_refused():
    with pytest.raises(ValueError, match="Only public"):
        identity.public_jwk("-----BEGIN PRIVATE KEY-----\nsecret-value\n")
    weak = (
        rsa.generate_private_key(public_exponent=65537, key_size=1024)
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    with pytest.raises(ValueError, match="2048"):
        identity.public_jwk(weak)


def signer_record(jwks):
    return {
        "schema_version": "klip-public-signing-input.v1",
        "cluster_id": CONFIG["cluster_id"],
        "captured_at": datetime.now(UTC).isoformat(),
        "jwks": jwks,
        "source_node_ids": {
            name: f"i-{index:017x}" for index, name in enumerate(CONFIG["nodes"], 1)
        },
        "source_public_key_sha256": {name: "a" * 64 for name in CONFIG["nodes"]},
    }


def test_signer_inputs_require_current_complete_provenance(tmp_path, monkeypatch, jwks):
    monkeypatch.setattr(identity, "state_home", lambda: tmp_path)
    value = signer_record(jwks)
    path = tmp_path / "cluster/public-signing-input.json"
    write_private(path, value)
    assert identity.signing_input() == jwks
    for change in (
        {"cluster_id": "other"},
        {"source_node_ids": {}},
        {
            "captured_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        },
    ):
        write_private(path, {**value, **change})
        with pytest.raises(ValueError):
            identity.signing_input()


def test_all_three_host_signers_must_match_the_advertised_keys(monkeypatch, public_pem, jwks):
    hosts = {
        name: {"klip_node": {"role": "control_plane", "instance_id": f"i-{index}"}}
        for index, name in enumerate(CONFIG["nodes"], 1)
    }
    nodes = {
        "items": [
            {"metadata": {"name": name}, "spec": {"providerID": f"aws:///az/i-{index}"}}
            for index, name in enumerate(CONFIG["nodes"], 1)
        ]
    }
    monkeypatch.setattr(cluster, "load_hosts", lambda: hosts)
    monkeypatch.setattr(cluster, "Host", lambda *args: SimpleNamespace(run=lambda *a: public_pem))
    monkeypatch.setattr(
        cluster,
        "kubectl",
        lambda *args: json.dumps(nodes if args[:2] == ("get", "nodes") else jwks),
    )
    assert identity.current_signing_keys()["jwks"] == jwks
    different = json.loads(
        (ROOT / "infra/terraform/environments/workload-identity/tests/public-jwks.json").read_text()
    )
    monkeypatch.setattr(
        cluster,
        "kubectl",
        lambda *args: json.dumps(nodes if args[:2] == ("get", "nodes") else different),
    )
    with pytest.raises(ValueError, match="advertised"):
        identity.current_signing_keys()
    monkeypatch.setattr(identity, "current_signing_keys", lambda: {"jwks": different})
    with pytest.raises(ValueError, match="changed"):
        identity.verify_signers_for_apply(jwks)


def test_recollect_does_not_silently_replace_published_keys(tmp_path, monkeypatch, jwks):
    other = json.loads(
        (ROOT / "infra/terraform/environments/workload-identity/tests/public-jwks.json").read_text()
    )
    monkeypatch.setattr(identity, "state_home", lambda: tmp_path)
    monkeypatch.setattr(identity, "current_signing_keys", lambda: signer_record(jwks))

    def call(*args, **kwargs):
        write_private(tmp_path / "cluster/published-signing-keys.json", other)
        return {"VersionId": "old"}

    monkeypatch.setattr(
        identity,
        "Aws",
        lambda *args: SimpleNamespace(
            preflight=lambda *args: None,
            call=call,
        ),
    )
    with pytest.raises(ValueError, match="rotation"):
        identity.collect()
    assert not (tmp_path / "cluster/public-signing-input.json").exists()
