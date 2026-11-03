import hashlib
import secrets
from datetime import timedelta
from functools import lru_cache
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select, text

from platform_management.api.dependencies import administrator
from platform_management.api.schemas import Login, SessionInfo
from platform_management.db.models import LoginAttempt, SessionRow
from platform_management.db.session import session
from platform_management.services.common import AppError, lock_key, now
from platform_management.settings import get_settings

router = APIRouter(prefix="/auth", tags=["Authentication"])
hasher = PasswordHasher()


@lru_cache
def password_hash():
    return hasher.hash(get_settings().secret(get_settings().admin_password_file, required=True))


@router.post("/login", response_model=SessionInfo, operation_id="login")
def login(body: Login, request: Request, response: Response):
    origin = request.headers.get("origin")
    if origin and origin != get_settings().allowed_origin:
        raise AppError(403, "Origin is not allowed")
    address = request.client.host if request.client else "local"
    with session() as db, db.begin():
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key("login:" + address)})
        count = db.scalar(
            select(func.count())
            .select_from(LoginAttempt)
            .where(
                LoginAttempt.address == address,
                LoginAttempt.created_at > now() - timedelta(minutes=10),
            )
        )
        if count >= 5:
            raise AppError(
                429, "Too many failed attempts. Try again in ten minutes.", "rate_limited"
            )
        try:
            valid = hasher.verify(password_hash(), body.password) and body.username == "admin"
        except VerifyMismatchError:
            valid = False
        if not valid:
            db.add(LoginAttempt(address=address))
        else:
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            expiry = now() + timedelta(hours=8)
            db.add(
                SessionRow(
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    csrf_token=csrf,
                    expires_at=expiry,
                )
            )
    if not valid:
        raise AppError(401, "Invalid username or password", "invalid_credentials")
    response.set_cookie(
        "management_session",
        token,
        httponly=True,
        secure=get_settings().secure_cookies,
        samesite="strict",
        max_age=8 * 3600,
        path="/api",
    )
    return SessionInfo(csrf_token=csrf, expires_at=expiry)


@router.get("/session", response_model=SessionInfo, operation_id="getSession")
def current(info: Annotated[dict, Depends(administrator)]):
    return info


@router.post("/logout", status_code=204, operation_id="logout")
def logout(request: Request, response: Response, info: Annotated[dict, Depends(administrator)]):
    token = request.cookies.get("management_session", "")
    with session() as db, db.begin():
        row = db.get(SessionRow, hashlib.sha256(token.encode()).hexdigest())
        if row:
            db.delete(row)
    response.delete_cookie("management_session", path="/api")
