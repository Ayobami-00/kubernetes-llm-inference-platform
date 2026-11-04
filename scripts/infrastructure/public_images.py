"""Resolve public release images to verified Linux AMD64 manifests without Docker.

Only anonymous, repository-scoped pull tokens are used. No user registry,
GitHub or AWS credentials are read, and tokens are never returned or retained.
"""

import hashlib
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit

MEDIA = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
DIGEST = r"sha256:[a-f0-9]{64}"
REFERENCE = re.compile(
    r"(docker.io|ghcr.io)/([a-z0-9][a-z0-9._/-]*)"
    r"(?::([a-zA-Z0-9_][a-zA-Z0-9._-]*)|@(" + DIGEST + r"))"
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def payload(url, *, token=None, redirects=False, limit=2 * 1024 * 1024):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    headers = {"User-Agent": "klip-public-image-resolver/1", "Accept": MEDIA}
    if token:
        headers["Authorization"] = "Bearer " + token
    for _ in range(4):
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as response:
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("Public registry response exceeded its download bound")
                return data, dict(response.headers.items())
        except urllib.error.HTTPError as error:
            location = error.headers.get("Location", "")
            parsed = urlsplit(location)
            allowed = parsed.hostname in {
                "production.cloudflare.docker.com",
                "production.cloudfront.docker.com",
                "pkg-containers.githubusercontent.com",
            } or (parsed.hostname or "").endswith(".r2.cloudflarestorage.com")
            if not (
                redirects
                and error.code in {301, 302, 303, 307, 308}
                and parsed.scheme == "https"
                and not parsed.username
                and not parsed.password
                and parsed.port in {None, 443}
                and allowed
            ):
                raise ValueError("Public registry read failed: HTTP " + str(error.code)) from None
            # A registry bearer token must never reach a blob CDN.
            headers.pop("Authorization", None)
            url = location
    raise ValueError("Public registry redirect limit exceeded")


def checked_json(data, expected=None, headers=None):
    actual = "sha256:" + hashlib.sha256(data).hexdigest()
    advertised = {k.lower(): v for k, v in (headers or {}).items()}.get("docker-content-digest")
    if (expected and actual != expected) or (advertised and actual != advertised):
        raise ValueError("Public registry content digest differs from its bytes")
    return json.loads(data), actual


def resolve(reference):
    match = REFERENCE.fullmatch(reference)
    if not match or match[3] in {"latest", "main", "master"} or ".." in match[2].split("/"):
        raise ValueError("An explicit public Docker Hub/GHCR release reference is required")
    registry, repository, tag, pinned = match.groups()
    host = "registry-1.docker.io" if registry == "docker.io" else registry
    realm = "https://auth.docker.io/token" if registry == "docker.io" else "https://ghcr.io/token"
    service = "registry.docker.io" if registry == "docker.io" else "ghcr.io"
    token_data, _ = payload(
        realm
        + "?"
        + urlencode({"service": service, "scope": "repository:" + repository + ":pull"}),
        limit=64 * 1024,
    )
    authorization = json.loads(token_data)
    token = authorization.get("token") or authorization.get("access_token")
    if not isinstance(token, str) or not token or len(token) > 32 * 1024:
        raise ValueError("Anonymous public registry authorization is unavailable")
    base = "https://" + host + "/v2/" + repository
    raw, headers = payload(base + "/manifests/" + (pinned or tag), token=token)
    document, parent_digest = checked_json(raw, pinned, headers)
    manifest_digest = parent_digest
    if "manifests" in document:
        candidates = [
            item
            for item in document["manifests"]
            if item.get("platform", {}).get("os") == "linux"
            and item.get("platform", {}).get("architecture") == "amd64"
            and item.get("platform", {}).get("variant", "") in {"", "v1"}
        ]
        if len(candidates) != 1 or not re.fullmatch(DIGEST, candidates[0].get("digest", "")):
            raise ValueError("Expected one unambiguous Linux AMD64 image manifest")
        child = candidates[0]
        raw, headers = payload(base + "/manifests/" + child["digest"], token=token)
        if len(raw) != child["size"]:
            raise ValueError("Image manifest descriptor size differs")
        document, manifest_digest = checked_json(raw, child["digest"], headers)
    if document.get("schemaVersion") != 2 or not document.get("layers"):
        raise ValueError("Expected a nonempty OCI/Docker image manifest")
    config = document.get("config", {})
    if not re.fullmatch(DIGEST, config.get("digest", "")):
        raise ValueError("Image configuration lacks an immutable digest")
    raw, _ = payload(base + "/blobs/" + config["digest"], token=token, redirects=True)
    body, _ = checked_json(raw, config["digest"])
    if (
        len(raw) != config["size"]
        or body.get("os") != "linux"
        or body.get("architecture") != "amd64"
    ):
        raise ValueError("Image config does not prove Linux AMD64 identity")
    for layer in document["layers"]:
        if not re.fullmatch(DIGEST, layer.get("digest", "")) or not isinstance(
            layer.get("size"), int
        ):
            raise ValueError("Image layer descriptor is invalid")
        if not 0 <= layer["size"] <= 32 * 1024**3:
            raise ValueError("Image layer exceeds the metadata accounting bound")
    return {
        "reference": reference,
        "source_manifest_digest": parent_digest,
        "digest": manifest_digest,
        "image": registry + "/" + repository + "@" + manifest_digest,
        "platform": "linux/amd64",
        "configuration_digest": config["digest"],
        "compressed_layer_bytes": sum(layer["size"] for layer in document["layers"]),
    }
