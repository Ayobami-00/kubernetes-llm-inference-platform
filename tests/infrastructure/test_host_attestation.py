"""Bootstrap material is scoped, deterministic and never transfers host private keys."""

import base64
import hashlib
import json

import pytest
import yaml
from platform_management.services.bootstrap import (
    SCRIPT_V1,
    SCRIPT_V2,
    SCRIPT_V3,
    SSH_GUARD,
    SSH_GUARD_V3,
    callback_origin,
    cloud_init,
    fingerprint,
    nonce_for,
    public_host_key,
    ssh_sources,
)

IDENTITY = "op-" + "1" * 16
ORIGIN = "https://management.example.test"
SOURCES = ["8.8.8.1/32", "8.8.8.2/32", "8.8.8.3/32"]


def test_existing_v1_cloud_init_bytes_remain_compatible_and_v2_sources_are_frozen():
    original = cloud_init(IDENTITY, "a" * 64, ORIGIN)
    assert (
        hashlib.sha256(original.encode()).hexdigest()
        == "4e7ebd36d83bbf38e96892264e79c2dfd3583ca74975d4619f1a402599f723ec"
    )
    content = cloud_init(IDENTITY, "a" * 64, ORIGIN, sources=SOURCES)
    old = cloud_init(IDENTITY, "a" * 64, ORIGIN, sources=SOURCES, program_version=2)
    assert hashlib.sha256(old.encode()).hexdigest() == (
        "0cf00d4bd9828ea4e32b21cbb8abe50d4ecab152b6945461a2df1df6f85b88c0"
    )
    assert content == cloud_init(IDENTITY, "a" * 64, ORIGIN, sources=list(reversed(SOURCES)))
    document = yaml.safe_load(content)
    files = {f["path"]: f for f in document["write_files"]}
    config = json.loads(files["/var/lib/klip/host-attestation.json"]["content"])
    assert config["ssh_sources"] == SOURCES
    assert files["/var/lib/klip/attest-host.py"]["content"] == SCRIPT_V3
    policy = files["/etc/ssh/sshd_config.d/10-klip-bootstrap.conf"]
    assert "AllowUsers ubuntu@8.8.8.1 ubuntu@8.8.8.2 ubuntu@8.8.8.3\n" in policy["content"]
    assert policy["permissions"] == "0600"
    compile(SCRIPT_V2, "attestation-v2", "exec")
    compile(SCRIPT_V3, "attestation-v3", "exec")


def test_v3_starts_native_ssh_service_before_testing_privilege_separation(monkeypatch):
    import subprocess

    commands = []

    def run(args, **kwargs):
        if args == ["/usr/sbin/sshd", "-t"]:
            assert commands == [["systemctl", "start", "ssh"]]
        commands.append(args)

    effective = (
        "permitrootlogin no\npasswordauthentication no\nkbdinteractiveauthentication no\n"
        "allowusers " + " ".join("ubuntu@" + s.split("/")[0] for s in SOURCES) + "\n"
    )
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", lambda *_a, **_kw: effective)
    namespace = {}
    exec(SSH_GUARD_V3, namespace)
    namespace["check_ssh_sources"](SOURCES)
    assert commands[-1] == ["systemctl", "reload-or-restart", "ssh"]


@pytest.mark.parametrize(
    "sources",
    [
        [],
        SOURCES[:2],
        [SOURCES[0]] * 3,
        ["0.0.0.0/0", *SOURCES[1:]],
        ["10.0.0.1/32", *SOURCES[1:]],
        ["8.8.8.1\nAllowUsers *", *SOURCES[1:]],
    ],
)
def test_bootstrap_cannot_render_broad_duplicate_or_injected_ssh_sources(sources):
    with pytest.raises(ValueError):
        ssh_sources(sources)


@pytest.mark.parametrize("combined", [True, False])
def test_actual_ssh_guard_accepts_only_effective_three_source_policy(monkeypatch, combined):
    import subprocess

    commands = []
    prefix = "permitrootlogin no\npasswordauthentication no\nkbdinteractiveauthentication no\n"
    users = ["ubuntu@" + s.split("/")[0] for s in SOURCES]
    expected = (
        "allowusers " + " ".join(users) + "\n"
        if combined
        else "".join("allowusers " + user + "\n" for user in users)
    )
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: commands.append((args, kw)))
    monkeypatch.setattr(subprocess, "check_output", lambda *_a, **_kw: prefix + expected)
    namespace = {}
    exec(compile(SSH_GUARD, "source-guard", "exec"), namespace)
    namespace["check_ssh_sources"](SOURCES)
    assert commands[-1][0] == ["systemctl", "reload-or-restart", "ssh"]
    assert commands[-1][1]["timeout"] == 30
    commands.clear()
    monkeypatch.setattr(subprocess, "check_output", lambda *_a, **_kw: prefix + "allowusers *\n")
    with pytest.raises(ValueError):
        namespace["check_ssh_sources"](SOURCES)
    assert all(args[0] != "systemctl" for args, _ in commands)


@pytest.mark.parametrize("safe", [True, False])
def test_actual_v2_program_attests_only_after_effective_ssh_guard(monkeypatch, capsys, safe):
    import io
    import pathlib
    import subprocess
    import types
    import urllib.request

    sent, removed, commands = [], [], []

    class FakePath:
        def __init__(self, path):
            self.path = path

        def read_text(self):
            if self.path.endswith("host-attestation.json"):
                return json.dumps(
                    {
                        "url": ORIGIN + "/api/v1/bootstrap/" + IDENTITY,
                        "nonce": "a" * 64,
                        "ssh_sources": SOURCES,
                    }
                )
            assert safe and self.path == "/etc/ssh/ssh_host_ed25519_key.pub"
            return key()

        def unlink(self):
            removed.append(self.path)

    class Response(io.BytesIO):
        status = 200

    def request(value, timeout):
        assert commands[-1] == ["systemctl", "reload-or-restart", "ssh"]
        sent.append(value)
        return Response(b"{}")

    prefix = "permitrootlogin no\npasswordauthentication no\nkbdinteractiveauthentication no\n"
    users = " ".join("ubuntu@" + s.split("/")[0] for s in SOURCES) if safe else "*"
    with monkeypatch.context() as patch:
        patch.setattr(pathlib, "Path", FakePath)
        patch.setattr(subprocess, "run", lambda args, **kw: commands.append(args))
        patch.setattr(
            subprocess, "check_output", lambda *_a, **_kw: prefix + "allowusers " + users + "\n"
        )
        patch.setattr(
            urllib.request, "build_opener", lambda *_: types.SimpleNamespace(open=request)
        )
        with pytest.raises(SystemExit) as result:
            exec(compile(SCRIPT_V2, "actual-source-restricted-bootstrap", "exec"), {})
    assert result.value.code == (0 if safe else 1)
    assert len(sent) == (1 if safe else 0)
    assert bool(removed) is safe
    captured = capsys.readouterr()
    assert "a" * 64 not in captured.out + captured.err


def test_actual_v3_program_reports_only_fixed_failure_without_attesting(monkeypatch, capsys):
    import io
    import pathlib
    import subprocess
    import types
    import urllib.request

    sent, removed = [], []

    class FakePath:
        def __init__(self, path):
            self.path = path

        def read_text(self):
            assert self.path == "/var/lib/klip/host-attestation.json"
            return json.dumps(
                {
                    "url": ORIGIN + "/api/v1/bootstrap/" + IDENTITY,
                    "nonce": "a" * 64,
                    "ssh_sources": SOURCES,
                }
            )

        def unlink(self):
            removed.append(self.path)

    def run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr="sensitive command output")

    def send(request, timeout):
        sent.append(request)
        assert request.full_url.endswith("/failure") and timeout == 10
        assert json.loads(request.data) == {"code": "ssh_policy"}
        return io.BytesIO(b'{"status":"recorded"}')

    with monkeypatch.context() as patch:
        patch.setattr(pathlib, "Path", FakePath)
        patch.setattr(subprocess, "run", run)
        patch.setattr(urllib.request, "build_opener", lambda *_: types.SimpleNamespace(open=send))
        with pytest.raises(SystemExit) as result:
            exec(compile(SCRIPT_V3, "actual-v3-bootstrap", "exec"), {})
    assert result.value.code == 1 and len(sent) == 1 and not removed
    captured = capsys.readouterr()
    assert "a" * 64 not in captured.out + captured.err
    assert "sensitive command output" not in captured.out + captured.err


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
