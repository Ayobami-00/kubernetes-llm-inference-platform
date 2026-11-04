"""Private verification keeps the intended hostname and rejects insecure TLS."""

import ssl
from urllib.parse import urlparse

import pytest
from management_acceptance import ORIGIN, PinnedTunnelConnection


def test_private_forwarding_rejects_unverified_tls_and_redirects():
    with pytest.raises(ValueError, match="another host"):
        PinnedTunnelConnection("unrelated.example", local_port=12345)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = PinnedTunnelConnection(
        urlparse(ORIGIN).hostname, local_port=12345, context=context
    )
    with pytest.raises(ValueError, match="verification"):
        connection.connect()


def test_private_forwarding_preserves_sni_without_external_dns(monkeypatch):
    import management_acceptance as acceptance

    context = ssl.create_default_context()
    calls = []
    raw, wrapped = object(), object()

    def connect(address, **kwargs):
        calls.append(address)
        return raw

    def wrap(sock, *, server_hostname):
        assert sock is raw and server_hostname == urlparse(ORIGIN).hostname
        return wrapped

    monkeypatch.setattr(acceptance.socket, "create_connection", connect)
    monkeypatch.setattr(context, "wrap_socket", wrap)
    connection = PinnedTunnelConnection(
        urlparse(ORIGIN).hostname, local_port=12345, context=context
    )
    connection.connect()
    assert calls == [("127.0.0.1", 12345)] and connection.sock is wrapped


def test_public_probe_uses_system_trust_without_a_private_ca_path(monkeypatch):
    from unittest.mock import Mock

    import management_acceptance as acceptance

    original = ssl.create_default_context
    paths = []

    def context(*, cafile):
        paths.append(cafile)
        return original(cafile=cafile)

    opener = Mock()
    opener.open.side_effect = RuntimeError("controlled transport interruption")
    monkeypatch.setattr(acceptance.ssl, "create_default_context", context)
    monkeypatch.setattr(acceptance.urllib.request, "build_opener", lambda *a: opener)
    with pytest.raises(RuntimeError, match="probe failed"):
        acceptance.https_probe(None, handler=Mock())
    assert paths == [None]


def test_failed_verification_persists_safe_classification(tmp_path, monkeypatch):
    import json
    import urllib.error

    import management_acceptance as acceptance

    (tmp_path / "plan.json").write_text(
        json.dumps(
            {"approval_sha256": "test-plan", "image_assessment": {"source_commit": "a" * 40}}
        )
    )
    (tmp_path / "active.json").write_text(json.dumps({"approval_sha256": "test-plan"}))
    monkeypatch.setattr(acceptance, "folder", lambda: tmp_path)

    def fail(plan, report, root, path):
        report["roles"] = {"api": "observed before the failure"}
        raise acceptance.HTTPSProbeFailure(
            "/health/ready", urllib.error.URLError(RuntimeError("synthetic-secret-message"))
        )

    monkeypatch.setattr(acceptance, "_verify", fail)
    with pytest.raises(acceptance.HTTPSProbeFailure):
        acceptance.verify()
    path = next(tmp_path.glob("verification/*/verification.json"))
    document = json.loads(path.read_text())
    assert document["status"] == "failed" and document["failed_at"]
    assert document["roles"]["api"] == "observed before the failure"
    assert document["error"]["type"] == "URLError"
    assert document["error"]["reason_type"] == "RuntimeError"
    assert "synthetic-secret-message" not in path.read_text()


def test_login_mutation_is_never_retried_and_response_text_is_not_retained(tmp_path, monkeypatch):
    import io
    import urllib.error
    from unittest.mock import Mock

    import management_acceptance as acceptance

    secret = tmp_path / "dev/.secrets/admin_password"
    secret.parent.mkdir(parents=True)
    secret.write_text("synthetic-password-never-logged")
    monkeypatch.setattr(acceptance, "ROOT", tmp_path)
    calls = []

    def open_request(request, **_):
        path = urlparse(request.full_url).path
        calls.append((request.get_method(), path))
        if path == "/health/ready":
            return io.BytesIO(b'{"status":"ok"}')
        raise urllib.error.HTTPError(request.full_url, 503, secret.read_text(), {}, None)

    opener = Mock()
    opener.open.side_effect = open_request
    monkeypatch.setattr(acceptance.urllib.request, "build_opener", lambda *a: opener)
    with pytest.raises(acceptance.HTTPSProbeFailure) as caught:
        acceptance.https_probe(None, handler=Mock())
    assert caught.value.safe_details["http_status"] == 503
    assert calls == [("GET", "/health/ready"), ("POST", "/api/v1/auth/login")]
    assert secret.read_text() not in str(caught.value)
