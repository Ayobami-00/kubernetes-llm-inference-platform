import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MANAGEMENT_", extra="ignore")
    database_url: str = "postgresql+psycopg://management:management@localhost:55432/management"
    database_require_tls: bool = False
    database_ca_file: Path | None = None
    admin_password_file: Path = Path("dev/.secrets/admin_password")
    lambda_api_key_file: Path = Path("dev/.secrets/lambda_api_key")
    secure_cookies: bool = False
    allowed_origin: str = "http://localhost:5173"
    simulation_step_seconds: float = 2.0
    repository_root: Path = Path(__file__).resolve().parents[5]
    artifacts_root: Path = Path("artifacts/management")
    simulation_runner: str = "simulated:v1"
    real_runner: str = "unconfigured:v1"
    runner_image: str = ""
    runner_command: list[str] = []
    runner_resources: dict[str, dict[str, str]] = {
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"cpu": "2", "memory": "2Gi"},
    }
    runner_node_selector: dict[str, str] = {"node-role.kubernetes.io/control-plane": ""}
    runner_tolerations: list[dict[str, str]] = [
        {
            "key": "node-role.kubernetes.io/control-plane",
            "operator": "Exists",
            "effect": "NoSchedule",
        }
    ]
    serving_namespace: str = "llm-d"
    runner_namespace: str = "klip"
    runner_secret_refs: list[str] = []
    runner_service_account: str = "klip-runner"
    artifact_claim: str = ""
    kube_api_url: str = ""
    kube_token_file: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    kube_ca_file: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    kube_allow_insecure_local: bool = False
    cluster_observation_max_age_seconds: int = 300
    import_max_bytes: int = 5 * 1024 * 1024
    runner_startup_allowance_seconds: int = 300
    lease_seconds: int = 60
    heartbeat_seconds: int = 10

    @field_validator("runner_resources")
    @classmethod
    def resource_bounds(cls, value):
        if set(value) != {"requests", "limits"} or any(
            set(v) != {"cpu", "memory"} for v in value.values()
        ):
            raise ValueError("runner resources require cpu/memory requests and limits")

        def amount(name, raw):
            pattern = (
                r"([0-9]+(?:\.[0-9]+)?)(m?)"
                if name == "cpu"
                else r"([0-9]+(?:\.[0-9]+)?)([KMGTPE]i?)?"
            )
            match = re.fullmatch(pattern, raw)
            if not match:
                raise ValueError("invalid runner resource quantity")
            number, unit = Decimal(match[1]), match[2] or ""
            if name == "cpu":
                return number / (1000 if unit == "m" else 1)
            return number * (1024 if unit.endswith("i") else 1000) ** (
                "KMGTPE".index(unit[0]) + 1 if unit else 0
            )

        for name in ("cpu", "memory"):
            requested, limit = (amount(name, value[k][name]) for k in ("requests", "limits"))
            if requested <= 0 or requested > limit:
                raise ValueError("runner requests must be positive and no greater than limits")
        return value

    def secret(self, path: Path, required: bool = False) -> str:
        value = path.read_text().strip() if path.exists() else ""
        if required and not value:
            raise RuntimeError(f"Required secret file is empty or missing: {path}")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
