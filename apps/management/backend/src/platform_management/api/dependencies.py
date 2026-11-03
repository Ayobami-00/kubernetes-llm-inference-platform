import hashlib
import hmac
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import APIKeyCookie
from sqlalchemy.orm import Session

from platform_management.db.models import SessionRow
from platform_management.db.session import session
from platform_management.services.common import AppError, now
from platform_management.settings import get_settings


def get_db():
    with session() as db, db.begin():
        yield db


Db = Annotated[Session, Depends(get_db)]


cookie_auth = APIKeyCookie(name="management_session", auto_error=False)


def administrator(request: Request, token: Annotated[str | None, Depends(cookie_auth)]):
    token = token or ""
    with session() as db:
        row = db.get(SessionRow, hashlib.sha256(token.encode()).hexdigest())
        if not row or row.expires_at <= now():
            raise AppError(401, "Sign in to continue", "unauthenticated")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not hmac.compare_digest(row.csrf_token, request.headers.get("X-CSRF-Token", "")):
                raise AppError(403, "CSRF token is missing or invalid", "csrf_failed")
            origin = request.headers.get("origin")
            if origin and origin != get_settings().allowed_origin:
                raise AppError(403, "Origin is not allowed", "origin_failed")
        return {"username": "admin", "csrf_token": row.csrf_token, "expires_at": row.expires_at}
