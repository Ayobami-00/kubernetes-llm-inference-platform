"""Bootstrap material is scoped, deterministic and never transfers host private keys."""

import base64

import pytest
import yaml
from platform_management.services.bootstrap import (
    SCRIPT_V1,
    callback_origin,
    cloud_init,
    fingerprint,
    nonce_for,
    public_host_key,
)

IDENTITY = "op-" + "1" * 16
ORIGIN = "https://management.example.test"


def key(value=b"a" * 32):
    return (
        "ssh-ed25519 "
        + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + value).decode()
    )


def test_public_host_key_has_exact_wire_format_and_stable_fingerprint():
    assert public_host_key(key()) == key()
    assert fingerprint(key()).startswith("SHA256:")
    assert fingerprint(key()) != fingerprint(key(b"b" * 32))
    for candidate in ("ssh-rsa AAAA", key() + "\n", key() + " comment", "PRIVATE KEY", ""):
        with pytest.raises(ValueError, match="public host key"):
            public_host_key(candidate)


def test_nonce_is_bound_to_seed_operation_origin_and_expiry():
    args = (b"x" * 32, IDENTITY, ORIGIN, "2026-11-04T00:00:00+00:00")
    first = nonce_for(*args)
    assert len(first) == 64 and first == nonce_for(*args)
    for changed in (
        (b"y" * 32, *args[1:]),
        (args[0], "op-" + "2" * 16, *args[2:]),
        (*args[:2], "https://different.example.test", args[3]),
        (*args[:3], "2026-11-04T00:01:00+00:00"),
    ):
        assert nonce_for(*changed) != first
    with pytest.raises(ValueError):
        nonce_for(b"short", *args[1:])


@pytest.mark.parametrize(
    "origin",
    [
        "http://management.example.test",
        "https://user:password@example.test",
        "https://example.test/path",
        "https://example.test/?token=secret",
        "https://example.test/#fragment",
    ],
)
def test_bootstrap_origin_cannot_contain_credentials_or_another_route(origin):
    with pytest.raises(ValueError):
        callback_origin(origin)


def test_cloud_init_uses_protected_files_https_and_no_shell_or_redirects():
    nonce = nonce_for(b"x" * 32, IDENTITY, ORIGIN, "2026-11-04T00:00:00+00:00")
    content = cloud_init(IDENTITY, nonce, ORIGIN)
    document = yaml.safe_load(content)
    assert document["ssh_pwauth"] is False and document["disable_root"] is True
    assert document["ssh_deletekeys"] is True and document["ssh_genkeytypes"] == ["ed25519"]
    assert all(
        f["owner"] == "root:root" and f["permissions"] == "0600" for f in document["write_files"]
    )
    assert document["runcmd"] == [["python3", "/var/lib/klip/attest-host.py"]]
    assert b"x" * 32 not in content.encode()
    assert "PRIVATE KEY" not in content and "ssh_host_ed25519_key.pub" in SCRIPT_V1
    assert "def redirect_request" in SCRIPT_V1 and "return None" in SCRIPT_V1
    compile(SCRIPT_V1, "attestation-v1", "exec")


@pytest.mark.parametrize(
    "outcome,exit_code,expected_calls",
    [
        ("accepted", 0, 1),
        ("unauthorized", 1, 1),
        ("unreachable", 1, 30),
    ],
)
def test_actual_bootstrap_program_sends_only_public_key_and_bounds_failures(
    monkeypatch, capsys, outcome, exit_code, expected_calls
):
    import builtins
    import io
    import json
    import types
    import urllib.error
    import urllib.request

    nonce = "a" * 64
    requested_paths, sent, removed, handlers_seen = [], [], [], []

    class FakePath:
        def __init__(self, value):
            self.value = value

        def read_text(self):
            requested_paths.append(self.value)
            if self.value == "/var/lib/klip/host-attestation.json":
                return json.dumps({"url": ORIGIN + "/api/v1/bootstrap/" + IDENTITY, "nonce": nonce})
            if self.value == "/etc/ssh/ssh_host_ed25519_key.pub":
                return key() + " root@example\n"
            pytest.fail("Bootstrap attempted to read unexpected or private host material")

        def unlink(self):
            removed.append(self.value)

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request, timeout):
            sent.append(request)
            assert timeout == 10
            assert request.full_url == ORIGIN + "/api/v1/bootstrap/" + IDENTITY
            assert request.headers["Authorization"] == "Bearer " + nonce
            assert json.loads(request.data) == {"host_key": key()}
            assert nonce not in request.full_url
            if outcome == "unauthorized":
                raise urllib.error.HTTPError(request.full_url, 401, "synthetic", {}, None)
            if outcome == "unreachable":
                raise urllib.error.URLError("synthetic unreachable")
            return Response(b'{"status":"accepted"}')

    def opener(*handlers):
        handlers_seen.extend(handlers)
        return Opener()

    original_import = builtins.__import__

    def imports(name, *args, **kwargs):
        return (
            types.SimpleNamespace(Path=FakePath)
            if name == "pathlib"
            else original_import(name, *args, **kwargs)
        )

    monkeypatch.setattr(urllib.request, "build_opener", opener)
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(SystemExit) as stopped:
        exec(SCRIPT_V1, {"__builtins__": {**vars(builtins), "__import__": imports}})
    assert stopped.value.code == exit_code and len(sent) == expected_calls
    assert requested_paths == [
        "/var/lib/klip/host-attestation.json",
        "/etc/ssh/ssh_host_ed25519_key.pub",
    ]
    assert bool(removed) == (outcome == "accepted")
    no_redirect = next(h for h in handlers_seen if h.__class__.__name__ == "NoRedirect")
    assert no_redirect.redirect_request(None, None, 302, "", {}, "https://attacker.invalid") is None
    assert nonce not in str(capsys.readouterr())
