from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AnyUrl, Field, SecretStr, field_validator, model_validator
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

    channel360_llm_provider: Literal["disabled", "openai_compatible"] = Field(
        default="disabled", validation_alias="CHANNEL360_LLM_PROVIDER")
    channel360_llm_api_key: SecretStr | None = Field(default=None, validation_alias="CHANNEL360_LLM_API_KEY")
    channel360_llm_model: str = Field(default="", validation_alias="CHANNEL360_LLM_MODEL")
    channel360_llm_base_url: str | None = Field(default=None, validation_alias="CHANNEL360_LLM_BASE_URL")
    channel360_llm_timeout_seconds: int = Field(default=20, validation_alias="CHANNEL360_LLM_TIMEOUT_SECONDS")
    channel360_llm_max_output_tokens: int = Field(default=500, validation_alias="CHANNEL360_LLM_MAX_OUTPUT_TOKENS")

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
        if not 1 <= self.channel360_llm_timeout_seconds <= 60:
            raise ValueError("CHANNEL360_LLM_TIMEOUT_SECONDS must be between 1 and 60")
        if not 64 <= self.channel360_llm_max_output_tokens <= 2000:
            raise ValueError("CHANNEL360_LLM_MAX_OUTPUT_TOKENS must be between 64 and 2000")
        if len(self.channel360_llm_model) > 200:
            raise ValueError("CHANNEL360_LLM_MODEL must be at most 200 characters")
        if self.channel360_llm_base_url is not None:
            from urllib.parse import urlsplit
            from ipaddress import ip_address
            parsed = urlsplit(self.channel360_llm_base_url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or
                    parsed.query or parsed.fragment or parsed.port not in (None, 443)):
                raise ValueError("CHANNEL360_LLM_BASE_URL must be a plain HTTPS endpoint without credentials or query")
            if parsed.hostname.lower() == "localhost" or parsed.hostname.lower().endswith((".localhost", ".local", ".internal")):
                raise ValueError("CHANNEL360_LLM_BASE_URL must not target a local or internal host")
            try:
                if not ip_address(parsed.hostname).is_global:
                    raise ValueError("CHANNEL360_LLM_BASE_URL must not target a private IP address")
            except ValueError as error:
                if "private IP" in str(error):
                    raise
        if self.channel360_llm_provider == "openai_compatible" and self.channel360_llm_base_url is None:
            raise ValueError("CHANNEL360_LLM_BASE_URL is required for the OpenAI-compatible provider")
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
