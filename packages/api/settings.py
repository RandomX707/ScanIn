from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AnyUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed runtime settings. Secret fields are redacted by Pydantic."""

    model_config = SettingsConfigDict(
        env_prefix="IDW_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["local", "test", "production"] = "local"
    auth_mode: Literal["development", "oidc"] = "development"
    development_auth_secret: SecretStr | None = None
    local_demo_auth_enabled: bool = False
    local_demo_tenant_id: UUID | None = None
    local_demo_tenant_name: str = "Channel360 Local Demo"
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: AnyUrl | None = None

    database_url: SecretStr = SecretStr("postgresql+psycopg://idw_app@127.0.0.1:5432/insurance_workbench")
    worker_database_url: SecretStr | None = None
    queue_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    service_name: str = "insurance-document-workbench"
    storage_root: str = ".runtime/document-storage"
    clamav_socket_path: str = "/var/run/clamav/clamd.ctl"
    max_upload_bytes: int = 50 * 1024 * 1024
    web_origins: list[str] = ["http://127.0.0.1:5173", "http://127.0.0.1:5174"]

    @field_validator("database_url")
    @classmethod
    def postgres_runtime_url(cls, value: SecretStr) -> SecretStr:
        from sqlalchemy.engine import make_url

        parsed = make_url(value.get_secret_value())
        if parsed.drivername != "postgresql+psycopg":
            raise ValueError("IDW_DATABASE_URL must use the PostgreSQL psycopg driver")
        return value

    @field_validator("queue_url")
    @classmethod
    def redis_compatible_url(cls, value: SecretStr) -> SecretStr:
        from sqlalchemy.engine import make_url

        parsed = make_url(value.get_secret_value())
        if parsed.drivername not in {"redis", "rediss"}:
            raise ValueError("IDW_QUEUE_URL must use a Redis-compatible redis:// or rediss:// URL")
        return value

    @field_validator("worker_database_url")
    @classmethod
    def worker_postgres_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            from sqlalchemy.engine import make_url
            if make_url(value.get_secret_value()).drivername != "postgresql+psycopg":
                raise ValueError("IDW_WORKER_DATABASE_URL must use the PostgreSQL psycopg driver")
        return value

    @model_validator(mode="after")
    def validate_security_configuration(self) -> "Settings":
        if not 1 <= self.max_upload_bytes <= 50 * 1024 * 1024:
            raise ValueError("IDW_MAX_UPLOAD_BYTES must be between 1 and 50 MiB")
        if not self.web_origins or any(not origin.startswith(("http://", "https://")) for origin in self.web_origins):
            raise ValueError("IDW_WEB_ORIGINS must contain explicit HTTP(S) origins")
        if self.auth_mode == "development":
            if self.environment == "production":
                raise ValueError("development authentication is forbidden in production")
            secret = self.development_auth_secret
            if secret is None or len(secret.get_secret_value().encode("utf-8")) < 32:
                raise ValueError("IDW_DEVELOPMENT_AUTH_SECRET must contain at least 32 bytes")
            if self.local_demo_auth_enabled and (
                self.environment != "local" or self.local_demo_tenant_id is None
            ):
                raise ValueError("local demo sign-in requires local environment and an explicit tenant ID")
            return self

        if self.local_demo_auth_enabled:
            raise ValueError("local demo sign-in is only available with development authentication")

        missing = [
            name
            for name, value in (
                ("OIDC_ISSUER", self.oidc_issuer),
                ("OIDC_AUDIENCE", self.oidc_audience),
                ("OIDC_JWKS_URL", self.oidc_jwks_url),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"OIDC authentication requires {', '.join(missing)}")
        if self.environment == "production":
            urls = (self.oidc_issuer, str(self.oidc_jwks_url))
            if any(not value.startswith("https://") for value in urls if value):
                raise ValueError("production OIDC issuer and JWKS URL must use HTTPS")
        return self


def get_settings() -> Settings:
    return Settings()
