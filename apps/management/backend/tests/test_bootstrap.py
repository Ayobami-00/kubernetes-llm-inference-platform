"""Nonce callbacks commit public identity before responding, across API replicas."""

import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
import yaml
from platform_management.db.models import ProviderIntent
from platform_management.db.session import session
from platform_management.services.bootstrap import accept, issue
from platform_management.services.common import AppError, now

IDENTITY = "op-" + "1" * 16
ORIGIN = "https://management.example.test"
SEED = b"synthetic-bootstrap-seed-value-0"


def key(value=b"a" * 32):
    return (
        "ssh-ed25519 "
        + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + value).decode()
    )


def prepare(tmp_path, *, mode="real"):
    path = tmp_path / "bootstrap-seed"
    path.write_bytes(SEED)
    with session() as db, db.begin():
        db.add(ProviderIntent(id=IDENTITY, group_id="real-group", payload={"mode": mode}))
    content, sha = issue(IDENTITY, ORIGIN, path)
    config = json.loads(yaml.safe_load(content)["write_files"][0]["content"])
    with session() as db, db.begin():
        row = db.get(ProviderIntent, IDENTITY)
        row.state = "dispatching"
    return path, content, sha, config["nonce"]


def test_callback_can_precede_allocation_response_but_requires_exact_nonce(
    client, tmp_path, caplog
):
    path, content, sha, nonce = prepare(tmp_path)
    assert hashlib.sha256(content.encode()).hexdigest() == sha
    # No administrator session or CSRF token is needed; the launch nonce authenticates.
    client.cookies.clear()
    route = "/api/v1/bootstrap/" + IDENTITY
    for header in (None, nonce, "Bearer wrong", "Basic " + nonce):
        response = client.post(
            route, json={"host_key": key()}, headers={"Authorization": header} if header else {}
        )
        assert response.status_code == 401
        assert nonce not in response.text
    first = client.post(
        route, json={"host_key": key()}, headers={"Authorization": "Bearer " + nonce}
    )
    assert first.status_code == 200 and first.json()["status"] == "accepted"
    with session() as db:
        recorded = db.get(ProviderIntent, IDENTITY).payload["bootstrap"]
        assert recorded["host_key"] == key() and recorded["accepted_at"]
        assert nonce not in json.dumps(recorded)
        assert SEED.decode() not in json.dumps(recorded)
    again = client.post(
        route, json={"host_key": key()}, headers={"Authorization": "Bearer " + nonce}
    )
    assert again.status_code == 200 and again.json() == first.json()
    conflict = client.post(
        route, json={"host_key": key(b"b" * 32)}, headers={"Authorization": "Bearer " + nonce}
    )
    assert conflict.status_code == 409
    with session() as db:
        assert db.get(ProviderIntent, IDENTITY).payload["bootstrap"] == recorded
    assert nonce not in caplog.text and SEED.decode() not in caplog.text


def test_bootstrap_issuance_is_immutable_and_separate_from_simulation(tmp_path):
    path = tmp_path / "seed"
    path.write_bytes(SEED)
    with session() as db, db.begin():
        db.add(ProviderIntent(id=IDENTITY, group_id="group", payload={"mode": "real"}))
    first = issue(IDENTITY, ORIGIN, path)
    assert issue(IDENTITY, ORIGIN, path) == first
    path.write_bytes(b"y" * 32)
    with pytest.raises(AppError, match="identity changed"):
        issue(IDENTITY, ORIGIN, path)
    with session() as db, db.begin():
        db.get(ProviderIntent, IDENTITY).payload = {"mode": "simulation"}
    with pytest.raises(AppError, match="real intent"):
        issue(IDENTITY, ORIGIN, path)


def test_source_restricted_bootstrap_pins_sources_without_rewriting_a_previous_issue(tmp_path):
    path = tmp_path / "seed"
    path.write_bytes(SEED)
    sources = ["8.8.8.1/32", "8.8.8.2/32", "8.8.8.3/32"]
    with session() as db, db.begin():
        db.add(ProviderIntent(id=IDENTITY, group_id="group", payload={"mode": "real"}))
    first = issue(IDENTITY, ORIGIN, path, sources=sources)
    assert issue(IDENTITY, ORIGIN, path, sources=list(reversed(sources))) == first
    with session() as db:
        value = db.get(ProviderIntent, IDENTITY).payload["bootstrap"]
        assert (
            value["schema_version"] == "klip-host-attestation.v2"
            and value["ssh_sources"] == sources
        )
    with pytest.raises(AppError, match="sources changed"):
        issue(IDENTITY, ORIGIN, path, sources=["8.8.8.4/32", *sources[1:]])
    with pytest.raises(AppError, match="sources changed"):
        issue(IDENTITY, ORIGIN, path)


@pytest.mark.parametrize("state", ["pending", "rejected", "terminated"])
def test_callback_does_not_activate_an_unlaunched_or_terminal_intent(client, tmp_path, state):
    _, _, _, nonce = prepare(tmp_path)
    with session() as db, db.begin():
        db.get(ProviderIntent, IDENTITY).state = state
    response = client.post(
        "/api/v1/bootstrap/" + IDENTITY,
        json={"host_key": key()},
        headers={"Authorization": "Bearer " + nonce},
    )
    assert response.status_code == 401


def test_expired_and_oversized_callbacks_are_rejected_without_overwriting_identity(
    client, tmp_path
):
    _, _, _, nonce = prepare(tmp_path)
    with session() as db, db.begin():
        row = db.get(ProviderIntent, IDENTITY)
        row.payload = {
            **row.payload,
            "bootstrap": {
                **row.payload["bootstrap"],
                "expires_at": (now() - timedelta(seconds=1)).isoformat(),
            },
        }
    route = "/api/v1/bootstrap/" + IDENTITY
    response = client.post(
        route, json={"host_key": key()}, headers={"Authorization": "Bearer " + nonce}
    )
    assert response.status_code == 401
    assert client.post(route, content=b"x" * 4097).status_code == 413
    assert client.post(route, content=iter([b"x" * 2048, b"x" * 2049])).status_code == 413
    with session() as db:
        assert "host_key" not in db.get(ProviderIntent, IDENTITY).payload["bootstrap"]


def test_concurrent_callbacks_cannot_replace_the_first_trusted_key(tmp_path):
    _, _, _, nonce = prepare(tmp_path)

    def callback(public_key):
        try:
            with session() as db, db.begin():
                return accept(db, IDENTITY, nonce, public_key)["status"]
        except AppError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(callback, [key(), key(b"b" * 32)]))
    assert sorted(statuses) == ["accepted", "host_identity_conflict"]
    with session() as db:
        recorded = db.get(ProviderIntent, IDENTITY).payload["bootstrap"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(callback, [recorded["host_key"]] * 2)) == ["accepted"] * 2
    with session() as db:
        assert db.get(ProviderIntent, IDENTITY).payload["bootstrap"] == recorded
