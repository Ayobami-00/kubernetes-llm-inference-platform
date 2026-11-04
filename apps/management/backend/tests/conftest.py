import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from platform_management.api.app import app
from platform_management.api.routers.auth import password_hash
from platform_management.db.models import Base
from platform_management.db.seed import seed
from platform_management.db.session import engine, session
from platform_management.settings import get_settings
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture(scope="session", autouse=True)
def test_database(tmp_path_factory):
    url = os.environ.get(
        "MANAGEMENT_TEST_DATABASE_URL",
        "postgresql+psycopg://management:management@localhost:55432/management_test",
    )
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    if not parsed.database or not parsed.database.endswith("_test"):
        raise RuntimeError("Tests require a dedicated database name ending in _test")
    bootstrap = create_engine(parsed.set(database="postgres"))
    with bootstrap.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        if not connection.scalar(
            text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": parsed.database}
        ):
            # Validated identifier, quoted independently from the connection URL.
            name = parsed.database.replace('"', '""')
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    bootstrap.dispose()
    secret = tmp_path_factory.mktemp("secrets") / "admin"
    secret.write_text("integration-test-password")
    os.environ["MANAGEMENT_DATABASE_URL"] = url
    os.environ["MANAGEMENT_ADMIN_PASSWORD_FILE"] = str(secret)
    os.environ["MANAGEMENT_LAMBDA_API_KEY_FILE"] = str(secret.parent / "absent")
    os.environ["MANAGEMENT_SIMULATION_STEP_SECONDS"] = "0"
    os.environ["MANAGEMENT_REAL_LIFECYCLE_ENABLED"] = "false"
    get_settings.cache_clear()
    engine.cache_clear()
    password_hash.cache_clear()
    command.upgrade(Config(str(ROOT / "apps/management/backend/alembic.ini")), "head")
    yield
    engine().dispose()


@pytest.fixture(autouse=True)
def clean_database(test_database):
    with engine().begin() as connection:
        tables = ",".join('"' + table.name + '"' for table in Base.metadata.sorted_tables)
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    with session() as db, db.begin():
        seed(db, ROOT)


@pytest.fixture
def client():
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "integration-test-password"},
        )
        assert response.status_code == 200, response.text
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        yield client
