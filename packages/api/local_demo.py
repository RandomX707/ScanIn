from __future__ import annotations

from ipaddress import ip_address
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from packages.api.auth import create_development_token
from packages.api.errors import APIError

router = APIRouter(prefix="/api/local-demo", tags=["local demo sign-in"])


class LocalDemoSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.post("/session")
def create_local_demo_session(payload: LocalDemoSessionRequest, request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    if not settings.local_demo_auth_enabled or settings.local_demo_tenant_id is None:
        raise APIError(404, "not_found", "Local demo sign-in is unavailable.")
    peer = request.client.host if request.client else ""
    try:
        is_loopback = ip_address(peer).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise APIError(404, "not_found", "Local demo sign-in is unavailable.")

    subject = "channel360-demo-owner"
    lifetime = 1800
    token = create_development_token(
        settings,
        subject=subject,
        tenant_id=settings.local_demo_tenant_id,
        role="OWNER",
        expires_in_seconds=lifetime,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": lifetime,
        "subject": subject,
        "role": "OWNER",
        "tenant_id": str(settings.local_demo_tenant_id),
        "tenant_name": settings.local_demo_tenant_name,
    }
