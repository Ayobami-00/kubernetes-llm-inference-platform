from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MANAGEMENT_", extra="ignore")
    database_url: str = "postgresql+psycopg://management:management@localhost:55432/management"
    admin_password_file: Path = Path("dev/.secrets/admin_password")
    lambda_api_key_file: Path = Path("dev/.secrets/lambda_api_key")
    secure_cookies: bool = False
    allowed_origin: str = "http://localhost:5173"
    simulation_step_seconds: float = 2.0
    repository_root: Path = Path(__file__).resolve().parents[5]
    artifacts_root: Path = Path("artifacts/management")
    lease_seconds: int = 60
    heartbeat_seconds: int = 10

    def secret(self, path: Path, required: bool = False) -> str:
        value = path.read_text().strip() if path.exists() else ""
        if required and not value:
            raise RuntimeError(f"Required secret file is empty or missing: {path}")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
