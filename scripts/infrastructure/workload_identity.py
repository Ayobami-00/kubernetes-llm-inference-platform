"""Public signing metadata for self-managed Kubernetes; never export a private signer."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import urllib.request
from datetime import UTC, datetime, timedelta

from common import CONFIG, PROFILES, ROOT, Aws, state_home, utc_now, write_private
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def configuration() -> dict:
    value = json.loads((ROOT / "infra/terraform/identity.json").read_text())
    if value != {
        "schema_version": "klip-workload-identity.v1",
        "issuer_hostname": "oidc." + CONFIG["domain"],
        "audience": "sts.amazonaws.com",
        "legacy_issuer": "https://kubernetes.default.svc.cluster.local",
        "token_expiration_seconds": 3600,
    }:
        raise ValueError("Unreviewed workload identity configuration")
    return value


def encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def decoded(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,1400}", value):
        raise ValueError("Invalid public signing-key encoding")
    result = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if encoded(result) != value:
        raise ValueError("Noncanonical public signing-key encoding")
    return result


def public_jwk(pem: str) -> dict:
    if "PRIVATE KEY" in pem or len(pem) > 20000:
        raise ValueError("Only public signing-key material may be exported")
    key = serialization.load_pem_public_key(pem.encode())
    if not isinstance(key, rsa.RSAPublicKey) or not 2048 <= key.key_size <= 8192:
        raise ValueError(
            "The initial identity profile requires an RSA public key of 2048-8192 bits"
        )
    numbers = key.public_numbers()
    if numbers.e != 65537:
        raise ValueError("Unreviewed RSA public exponent")
    der = key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return {
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "kid": encoded(hashlib.sha256(der).digest()),
        "n": encoded(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": encoded(numbers.e.to_bytes(3, "big")),
    }


def validate_jwks(document: dict) -> dict:
    if (
        not isinstance(document, dict)
        or set(document) != {"keys"}
        or not isinstance(document["keys"], list)
        or not 1 <= len(document["keys"]) <= 20
    ):
        raise ValueError("Expected 1-20 verified public signing keys")
    ids = set()
    for item in document["keys"]:
        if not isinstance(item, dict) or set(item) != {"kty", "alg", "use", "kid", "n", "e"}:
            raise ValueError("Unexpected signing-key fields; private parameters are forbidden")
        if not all(isinstance(value, str) for value in item.values()):
            raise ValueError("Invalid signing-key values")
        numbers = rsa.RSAPublicNumbers(
            int.from_bytes(decoded(item["e"]), "big"), int.from_bytes(decoded(item["n"]), "big")
        )
        pem = (
            numbers.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )
        if public_jwk(pem) != item or item["kid"] in ids:
            raise ValueError("Signing key ID/algorithm mismatch or duplicate public key")
        ids.add(item["kid"])
    return document


def current_signing_keys() -> dict:
    from cluster import Host, kubectl, load_hosts

    hosts = load_hosts()
    nodes = {
        node["metadata"]["name"]: node
        for node in json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    }
    keys, hashes, instances = {}, {}, {}
    for name in sorted(CONFIG["nodes"]):
        fields = hosts[name]
        identifier = fields["klip_node"]["instance_id"]
        if (
            fields["klip_node"]["role"] != "control_plane"
            or nodes[name]["spec"].get("providerID", "").split("/")[-1] != identifier
        ):
            raise ValueError("Control-plane signer identity does not match the live cluster")
        pem = Host(fields).run(["sudo", "cat", "/etc/kubernetes/pki/sa.pub"])
        keys[name] = public_jwk(pem)
        hashes[name] = hashlib.sha256(pem.encode()).hexdigest()
        instances[name] = identifier
    if len({json.dumps(key, sort_keys=True) for key in keys.values()}) != 1:
        raise ValueError("The control planes disagree on the active public signer")
    advertised = validate_jwks(json.loads(kubectl("get", "--raw", "/openid/v1/jwks")))
    key = next(iter(keys.values()))
    if key not in advertised["keys"]:
        raise ValueError("Kubernetes advertised signing keys disagree with its public signer")
    return {
        "schema_version": "klip-public-signing-input.v1",
        "cluster_id": CONFIG["cluster_id"],
        "captured_at": utc_now(),
        "source_node_ids": instances,
        "source_public_key_sha256": hashes,
        "jwks": advertised,
    }


def collect() -> dict:
    config = configuration()
    aws = Aws(PROFILES["bootstrap"])
    aws.preflight("bootstrap")
    value = current_signing_keys()
    # Existing published keys are retained. Rotation requires a reviewed overlap
    # procedure; this command must not silently invalidate existing workload tokens.
    path = state_home() / "cluster/published-signing-keys.json"
    write_private(path, "")
    existing = aws.call(
        "s3api",
        "get-object",
        "--bucket",
        f"klip-identity-{CONFIG['account_id']}-{CONFIG['region']}",
        "--key",
        "openid/v1/jwks",
        "--expected-bucket-owner",
        CONFIG["account_id"],
        str(path),
        missing=True,
    )
    if existing is not None:
        published = validate_jwks(json.loads(path.read_text()))
        if published != value["jwks"]:
            raise ValueError(
                "Published signer differs; use an explicit signing-key rotation procedure"
            )
    else:
        provider = aws.call(
            "iam",
            "get-open-id-connect-provider",
            "--open-id-connect-provider-arn",
            f"arn:aws:iam::{CONFIG['account_id']}:oidc-provider/{config['issuer_hostname']}",
            missing=True,
        )
        if provider is not None:
            raise ValueError("Existing IAM issuer lost its published keys; inspect retained state")
    write_private(state_home() / "cluster/public-signing-input.json", value)
    return {
        "key_ids": [key["kid"] for key in value["jwks"]["keys"]],
        "captured_at": value["captured_at"],
    }


def signing_input() -> dict:
    configuration()
    value = json.loads((state_home() / "cluster/public-signing-input.json").read_text())
    if set(value) != {
        "schema_version",
        "cluster_id",
        "captured_at",
        "source_node_ids",
        "source_public_key_sha256",
        "jwks",
    } or (value["schema_version"], value["cluster_id"]) != (
        "klip-public-signing-input.v1",
        CONFIG["cluster_id"],
    ):
        raise ValueError("Unexpected public signing input identity")
    if set(value["source_node_ids"]) != set(CONFIG["nodes"]) or set(
        value["source_public_key_sha256"]
    ) != set(CONFIG["nodes"]):
        raise ValueError("Public signing input must identify all three control-plane sources")
    timestamp = datetime.fromisoformat(value["captured_at"])
    if timestamp.tzinfo is None or not timedelta(minutes=-5) <= datetime.now(
        UTC
    ) - timestamp <= timedelta(hours=1):
        raise ValueError("Public signing input is stale; recollect from the live control planes")
    return validate_jwks(value["jwks"])


def verify_signers_for_apply(expected: dict) -> None:
    if validate_jwks(expected) != current_signing_keys()["jwks"]:
        raise ValueError("Live Kubernetes signer changed since identity planning; apply refused")


def verify_public() -> dict:
    config = configuration()
    issuer = "https://" + config["issuer_hostname"]
    documents = {}
    for path in (".well-known/openid-configuration", "openid/v1/jwks"):
        with urllib.request.urlopen(issuer + "/" + path, timeout=15) as response:
            if response.geturl() != issuer + "/" + path:
                raise ValueError("Identity metadata must not redirect to another endpoint")
            documents[path] = json.loads(response.read(65537))
    discovery = documents[".well-known/openid-configuration"]
    if discovery["issuer"] != issuer or discovery["jwks_uri"] != issuer + "/openid/v1/jwks":
        raise ValueError("Published issuer metadata differs from the configured identity")
    if validate_jwks(documents["openid/v1/jwks"]) != signing_input():
        raise ValueError("Publicly served keys disagree with the verified Kubernetes signer")
    aws = Aws(PROFILES["bootstrap"])
    aws.preflight("bootstrap")
    provider = aws.call(
        "iam",
        "get-open-id-connect-provider",
        "--open-id-connect-provider-arn",
        f"arn:aws:iam::{CONFIG['account_id']}:oidc-provider/{config['issuer_hostname']}",
    )
    if provider["Url"].removeprefix("https://") != config["issuer_hostname"] or provider[
        "ClientIDList"
    ] != [config["audience"]]:
        raise ValueError("IAM issuer/audience differs from the configured identity")
    result = {
        "captured_at": utc_now(),
        "issuer": issuer,
        "public_metadata_verified": True,
        "key_ids": [key["kid"] for key in documents["openid/v1/jwks"]["keys"]],
        "scope": "public_metadata_and_iam_provider",
    }
    write_private(state_home() / "cluster/workload-identity-public-verification.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["collect", "verify-public"])
    args = parser.parse_args()
    print(json.dumps(collect() if args.command == "collect" else verify_public(), indent=2))
