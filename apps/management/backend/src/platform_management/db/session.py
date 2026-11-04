from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from platform_management.settings import get_settings


@lru_cache
def engine():
    settings = get_settings()
    connect_args = {}
    if settings.database_require_tls:
        mode = make_url(settings.database_url).query.get("sslmode")
        if mode not in (None, "verify-full"):
            raise ValueError("The database URL conflicts with required verified TLS")
        if settings.database_ca_file is None or not settings.database_ca_file.is_file():
            raise ValueError("Verified database TLS requires a mounted CA file")
        connect_args = {"sslmode": "verify-full", "sslrootcert": str(settings.database_ca_file)}
    return create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)


def session():
    return sessionmaker(engine(), expire_on_commit=False)()
