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
