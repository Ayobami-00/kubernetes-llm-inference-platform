"""Rooted local artifacts; unsupported schemes are never fetched."""

import hashlib
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse


class ArtifactAccess(Protocol):
    def verify(self, uri: str, sha256: str) -> bool: ...


class LocalArtifactAccess:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def path(self, uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.query or parsed.fragment:
            raise ValueError("Artifact queries and fragments are not supported")
        if parsed.scheme == "local":
            candidate = self.root / unquote(parsed.netloc + parsed.path).lstrip("/")
        elif parsed.scheme == "file" and parsed.netloc in {"", "localhost"}:
            candidate = Path(unquote(parsed.path))
        else:
            raise ValueError("Only staged local:// and file:// artifacts are supported")
        candidate = candidate.resolve()
        if not candidate.is_relative_to(self.root) or not candidate.is_file():
            raise ValueError("Artifact is absent or outside the configured artifact root")
        return candidate

    def verify(self, uri, sha256):
        try:
            path = self.path(uri)
            with path.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest() == sha256
        except (ValueError, OSError):
            return False
