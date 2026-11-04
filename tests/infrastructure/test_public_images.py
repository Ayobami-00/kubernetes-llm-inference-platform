"""Public image pins prove content/platform identity without a Docker credential."""

import hashlib
import json
import urllib.error

import public_images as images
import pytest


def encode(value):
    data = json.dumps(value).encode()
    return data, "sha256:" + hashlib.sha256(data).hexdigest()


def registry(monkeypatch, *, architecture="amd64", duplicate=False, bad_size=False):
    config, config_sha = encode({"architecture": architecture, "os": "linux"})
    manifest, sha = encode(
        {
            "schemaVersion": 2,
            "config": {"digest": config_sha, "size": len(config)},
            "layers": [{"digest": "sha256:" + "a" * 64, "size": 1234}],
        }
    )
    descriptor = {
        "digest": sha,
        "size": len(manifest) + int(bad_size),
        "platform": {"os": "linux", "architecture": "amd64"},
    }
    index, index_sha = encode({"manifests": [descriptor] * (2 if duplicate else 1)})
    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        if "/token?" in url:
            assert "scope=repository%3Aenvoyproxy%2Fgateway%3Apull" in url
            assert "token" not in kwargs
            return b'{"token":"synthetic-anonymous-token"}', {}
        assert kwargs["token"] == "synthetic-anonymous-token"
        if url.endswith("/v1.9.3"):
            return index, {"Docker-Content-Digest": index_sha}
        if url.endswith("/" + sha):
            return manifest, {"Docker-Content-Digest": sha}
        assert url.endswith("/" + config_sha) and kwargs["redirects"]
        return config, {}

    monkeypatch.setattr(images, "payload", fetch)
    return calls, sha, index_sha


def test_multiarch_release_resolves_to_content_verified_amd64_without_layers(monkeypatch):
    calls, sha, index_sha = registry(monkeypatch)
    result = images.resolve("docker.io/envoyproxy/gateway:v1.9.3")
    assert result["image"] == "docker.io/envoyproxy/gateway@" + sha
    assert result["source_manifest_digest"] == index_sha
    assert result["platform"] == "linux/amd64" and result["compressed_layer_bytes"] == 1234
    assert "synthetic-anonymous-token" not in json.dumps(result)
    assert len(calls) == 4


@pytest.mark.parametrize(
    "options,reason",
    [
        ({"architecture": "arm64"}, "config"),
        ({"duplicate": True}, "unambiguous"),
        ({"bad_size": True}, "size"),
    ],
)
def test_registry_descriptor_or_platform_disagreement_is_rejected(monkeypatch, options, reason):
    registry(monkeypatch, **options)
    with pytest.raises(ValueError, match=reason):
        images.resolve("docker.io/envoyproxy/gateway:v1.9.3")


@pytest.mark.parametrize(
    "reference",
    [
        "docker.io/envoyproxy/gateway:latest",
        "ghcr.io/repo/image:main",
        "docker.io/envoyproxy/gateway",
        "https://user:password@registry.example/image:tag",
        "localhost:5000/image:tag",
        "ghcr.io/../other:v1",
    ],
)
def test_unversioned_private_or_injected_references_fail_before_http(monkeypatch, reference):
    monkeypatch.setattr(images, "payload", lambda *a, **k: pytest.fail("Unexpected HTTP"))
    with pytest.raises(ValueError):
        images.resolve(reference)


def test_changed_content_is_rejected_even_if_registry_advertises_original_digest():
    raw, original = encode({"os": "linux"})
    with pytest.raises(ValueError, match="digest"):
        images.checked_json(raw + b" ", headers={"Docker-Content-Digest": original})


@pytest.mark.parametrize(
    "location", ["https://production.cloudflare.docker.com/blob", "https://evil.example/blob"]
)
def test_blob_redirects_never_forward_registry_authorization(monkeypatch, location):
    import io
    from types import SimpleNamespace

    requests = []

    class Response(io.BytesIO):
        headers = {}

    def open_request(request, **kwargs):
        requests.append(request)
        if len(requests) == 1:
            raise urllib.error.HTTPError(
                request.full_url, 307, "redirect", {"Location": location}, None
            )
        assert request.get_header("Authorization") is None
        return Response(b"{}")

    monkeypatch.setattr(
        images.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=open_request)
    )
    if "evil" in location:
        with pytest.raises(ValueError, match="HTTP 307"):
            images.payload(
                "https://registry-1.docker.io/v2/example/blobs/id", token="test", redirects=True
            )
        assert len(requests) == 1
    else:
        assert (
            images.payload(
                "https://registry-1.docker.io/v2/example/blobs/id", token="test", redirects=True
            )[0]
            == b"{}"
        )
        assert len(requests) == 2
