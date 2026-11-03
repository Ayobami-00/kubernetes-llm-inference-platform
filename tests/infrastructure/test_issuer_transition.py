"""Two-phase issuer changes preserve keys, encryption and current token validation."""

import base64
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from cluster_config import kubeadm_documents
from issuer_transition import (
    bootstrap_extra_args,
    restore_owned_manifest,
    settings,
    token_claims,
    transition_command,
)
from workload_identity import configuration

LEGACY = configuration()["legacy_issuer"]
PUBLIC = "https://" + configuration()["issuer_hostname"]


@pytest.fixture
def original():
    return [
        "kube-apiserver",
        "--advertise-address=10.200.0.11",
        "--encryption-provider-config=/etc/klip/encryption.yaml",
        "--service-account-issuer=" + LEGACY,
        "--service-account-key-file=/etc/kubernetes/pki/sa.pub",
        "--service-account-signing-key-file=/etc/kubernetes/pki/sa.key",
    ]


def issuers(command):
    return [
        item.split("=", 1)[1] for item in command if item.startswith("--service-account-issuer=")
    ]


def test_acceptance_precedes_new_token_issuance_and_preserves_unrelated_flags(original):
    accepted = transition_command(original, "accept")
    assert issuers(accepted) == [LEGACY, PUBLIC]
    primary = transition_command(accepted, "primary")
    assert issuers(primary) == [PUBLIC, LEGACY]
    for flag in original:
        if not flag.startswith("--service-account-issuer="):
            assert flag in accepted and flag in primary
    assert transition_command(primary, "accept") == primary
    assert transition_command(primary, "primary") == primary
    assert "--api-audiences=" + LEGACY + "," + PUBLIC in primary
    assert "--service-account-jwks-uri=" + PUBLIC + "/openid/v1/jwks" in primary


@pytest.mark.parametrize(
    "replacement",
    [
        "--service-account-issuer=https://unreviewed.example.com",
        "--api-audiences=unreviewed-audience",
        "--api-audiences",
    ],
)
def test_foreign_issuer_or_audience_requires_review(original, replacement):
    command = [item for item in original if not item.startswith("--service-account-issuer=")]
    if not replacement.startswith("--service-account-issuer="):
        command.append("--service-account-issuer=" + LEGACY)
    with pytest.raises(ValueError):
        transition_command([*command, replacement], "primary")


def test_a_signing_key_path_change_is_not_hidden_in_the_transition(original):
    command = [item.replace("/pki/sa.key", "/pki/other.key") for item in original]
    with pytest.raises(ValueError, match="signing-key"):
        transition_command(command, "accept")


@pytest.mark.parametrize(
    "actual,attempted,expected",
    [
        ("ours", True, "restored"),
        ("original", True, "unchanged"),
        ("other-operator-change", True, "blocked_by_drift"),
        ("ours", False, "not_attempted"),
    ],
)
def test_rollback_restores_only_our_attempted_write(actual, attempted, expected):
    writes = []
    host = SimpleNamespace(
        run=lambda *args: hashlib.sha256(actual.encode()).hexdigest() + " file",
        put=lambda *args: writes.append(args),
    )
    assert restore_owned_manifest(host, "original", "ours", attempted) == expected
    assert bool(writes) is (expected == "restored")


def test_bootstrap_preserves_repeated_v1beta4_args_and_encryption(allocation):
    from cluster_config import node_configuration

    config = node_configuration("cp-01", allocation, allocation["nodes"]["cp-01"])
    documents = kubeadm_documents(config)
    cluster = next(item for item in documents if item["kind"] == "ClusterConfiguration")
    args = cluster["apiServer"]["extraArgs"]
    assert {"name": "encryption-provider-config", "value": "/etc/klip/encryption.yaml"} in args
    assert [item["value"] for item in args if item["name"] == "service-account-issuer"] == [
        PUBLIC,
        LEGACY,
    ]
    assert bootstrap_extra_args(args) == args
    assert settings(PUBLIC)[-1]["name"] == "api-audiences"


def test_token_metadata_checks_are_explicitly_not_signature_validation():
    # Synthetic parser fixture. The live workflow must additionally use TokenReview.
    header = {"alg": "RS256", "kid": "k" * 43}
    payload = {"iss": PUBLIC, "aud": [LEGACY]}

    def token(claims):
        def encoded(value):
            return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

        return encoded(header) + "." + encoded(claims) + ".synthetic"

    assert token_claims(token(payload), PUBLIC, LEGACY, [header["kid"]])["issuer"] == PUBLIC
    for field, value in (("iss", LEGACY), ("aud", ["sts.amazonaws.com"])):
        invalid = copy.deepcopy(payload)
        invalid[field] = value
        with pytest.raises(ValueError, match="issuer/audience"):
            token_claims(token(invalid), PUBLIC, LEGACY, [header["kid"]])
