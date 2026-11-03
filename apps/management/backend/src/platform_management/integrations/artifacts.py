import hashlib
from pathlib import Path
from typing import Protocol


class ArtifactAccess(Protocol):
    def verify(self, uri: str, sha256: str) -> bool: ...


class LocalArtifactAccess:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def verify(self, uri, sha256):
        if not uri.startswith("local://"):
            return False
        path = (self.root / uri.removeprefix("local://")).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            return False
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest() == sha256
