from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import UUID

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient, PyJWKClientError
from pydantic import BaseModel, ConfigDict, ValidationError

from packages.api.errors import APIError
from packages.api.settings import Settings

Role = Literal["processor", "reviewer", "administrator"]
bearer_scheme = HTTPBearer(auto_error=False, bearerFormat="JWT")


class AuthenticatedIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str
    tenant_id: UUID
    role: Role


def create_development_token(
    settings: Settings,
    *,
    subject: str,
    tenant_id: UUID,
    role: Role,
    expires_in_seconds: int = 900,
) -> str:
    """Issue a short-lived local token; this method is disabled outside local/test."""
    if settings.auth_mode != "development" or settings.environment not in {"local", "test"}:
        raise RuntimeError("development tokens are unavailable outside local/test environments")
    if not 1 <= expires_in_seconds <= 3600:
        raise ValueError("development tokens may live for at most one hour")
    now = datetime.now(timezone.utc)
    claims = {
        "sub": subject,
        "tenant_id": str(tenant_id),
        "role": role,
        "iss": "idw-local-development",
        "aud": "idw-api",
        "iat": now,
        "exp": now + timedelta(seconds=expires_in_seconds),
    }
    assert settings.development_auth_secret is not None
    return jwt.encode(claims, settings.development_auth_secret.get_secret_value(), algorithm="HS256")


def _decode_claims(settings: Settings, token: str) -> dict[str, object]:
    if settings.auth_mode == "development":
        if settings.environment not in {"local", "test"} or settings.development_auth_secret is None:
            raise APIError(401, "invalid_identity", "Authentication failed.")
        return jwt.decode(
            token,
            settings.development_auth_secret.get_secret_value(),
            algorithms=["HS256"],
            issuer="idw-local-development",
            audience="idw-api",
            options={"require": ["sub", "tenant_id", "role", "exp", "iat"]},
        )

    assert settings.oidc_jwks_url is not None
    assert settings.oidc_issuer is not None
    assert settings.oidc_audience is not None
    signing_key = PyJWKClient(str(settings.oidc_jwks_url), timeout=3).get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        options={"require": ["sub", "tenant_id", "role", "exp", "iat"]},
    )


def require_identity(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> AuthenticatedIdentity:
    """Return a verified actor. No tenant or role header is accepted as authority."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise APIError(401, "authentication_required", "A valid bearer identity is required.")
    settings: Settings = request.app.state.settings
    try:
        claims = _decode_claims(settings, credentials.credentials)
        return AuthenticatedIdentity(
            subject=str(claims["sub"]),
            tenant_id=UUID(str(claims["tenant_id"])),
            role=claims["role"],
        )
    except (jwt.PyJWTError, PyJWKClientError, KeyError, ValueError, ValidationError, TypeError):
        raise APIError(401, "invalid_identity", "Authentication failed.") from None
