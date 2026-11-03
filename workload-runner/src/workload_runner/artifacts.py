"""Checksummed, atomic local artifacts. Readers never observe half-written status files."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=lambda obj: obj.model_dump(mode="json"),
    ).encode()


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(json.loads(canonical(value)), indent=2, sort_keys=True) + "\n"
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_jsonl(path: Path, records) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b"".join(canonical(record) + b"\n" for record in records)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    return digest(data)


def read_jsonl(path: Path, model=None):
    with path.open() as stream:
        for line in stream:
            if line.strip():
                yield model.model_validate_json(line) if model else json.loads(line)


def artifact_path(uri: str, expected_sha256: str | None = None) -> Path:
    parsed = urlparse(str(uri))
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise ValueError("execution requires staged local file:// artifacts")
    path = Path(unquote(parsed.path))
    if expected_sha256 and digest(path.read_bytes()) != expected_sha256:
        raise ValueError(f"artifact checksum mismatch: {path.name}")
    return path
