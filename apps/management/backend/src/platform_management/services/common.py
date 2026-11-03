import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4


class AppError(Exception):
    def __init__(self, status: int, message: str, code: str = "invalid_request"):
        self.status, self.message, self.code = status, message, code


def now():
    return datetime.now(UTC)


def identifier(prefix):
    return f"{prefix}-{uuid4().hex[:16]}"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def lock_key(value):
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big", signed=True)
