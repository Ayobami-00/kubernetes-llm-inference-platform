"""Cleanup must be UID-conditional at the API, including name-reuse conflicts."""

import base64
import http.client
import io
import json
import ssl
import subprocess
import sys
from types import SimpleNamespace

import cluster
import pytest


@pytest.mark.parametrize("status", [200, 202, 404, 409, 403])
@pytest.mark.parametrize(
    "target",
    [
        "/apis/batch/v1/namespaces/klip/jobs/storage-test",
        "/apis/postgresql.cnpg.io/v1/namespaces/platform-data/clusters/klip-db-restore",
        "/api/v1/namespaces/klip/pods/klip-api-1234-abcd",
        "/api/v1/namespaces/platform-data/pods/klip-db-1",
    ],
)
def test_remote_cleanup_enforces_the_uid_and_never_hides_api_conflicts(monkeypatch, status, target):
    uid = "00000000-0000-0000-0000-000000000001"
    requests = []
    certificate = base64.b64encode(b"synthetic certificate fixture").decode()
    config = {
        "clusters": [
            {
                "cluster": {
                    "server": "https://api.example.test:6443",
                    "certificate-authority-data": certificate,
                }
            }
        ],
        "users": [
            {"user": {"client-certificate-data": certificate, "client-key-data": certificate}}
        ],
    }
    context = SimpleNamespace(load_cert_chain=lambda *args: None)

    class Connection:
        def __init__(self, host, port, **kwargs):
            assert host == "api.example.test" and port == 6443
            assert kwargs["context"] is context

        def request(self, method, path, body, headers):
            requests.append((method, path, json.loads(body)))

        def getresponse(self):
            return SimpleNamespace(status=status, read=lambda: b"")

    def remote(arguments, *, data, secret):
        assert secret
        with monkeypatch.context() as patch:
            patch.setattr(sys, "stdin", io.StringIO(data))
            patch.setattr(
                subprocess, "check_output", lambda *args, **kwargs: json.dumps(config).encode()
            )
            patch.setattr(ssl, "create_default_context", lambda **kwargs: context)
            patch.setattr(http.client, "HTTPSConnection", Connection)
            exec(arguments[-1], {})

    monkeypatch.setattr(cluster, "load_hosts", lambda: {"cp-01": {}})
    monkeypatch.setattr(cluster, "Host", lambda _: SimpleNamespace(run=remote))
    if status in {409, 403}:
        with pytest.raises(RuntimeError, match=str(status)):
            cluster.delete_owned_resource(target, uid)
    else:
        cluster.delete_owned_resource(target, uid)
    assert requests == [
        (
            "DELETE",
            target,
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "propagationPolicy": "Foreground",
                "preconditions": {"uid": uid},
            },
        )
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/nodes/other",
        "/api/v1/namespaces/a/../b",
        "/api/v1/namespaces/klip/pods/klip-worker-1234-abcd",
        "/api/v1/namespaces/another/pods/klip-api-1234-abcd",
        "/api/v1/namespaces/platform-data/pods/unrelated-db-1",
        "/apis/postgresql.cnpg.io/v1/namespaces/platform-data/clusters/klip-db",
    ],
)
def test_cleanup_cannot_be_redirected_to_another_resource_kind(path):
    with pytest.raises(ValueError, match="cleanup target"):
        cluster.delete_owned_resource(path, "00000000-0000-0000-0000-000000000001")
