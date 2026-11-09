from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy import text

from packages.api.auth import AuthenticatedIdentity, require_identity
from packages.api.db_context import api_tenant_transaction
from packages.registry.documents import DocumentRepository

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/summary")
def dashboard_summary(request: Request,
                      identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        snapshot = DocumentRepository(connection, identity.tenant_id).dashboard_snapshot()
        # Row-level security limits this to the caller's own tenant row.
        tenant_name = connection.execute(text("SELECT name FROM tenants WHERE id=:tenant"),
                                         {"tenant": identity.tenant_id}).scalar_one_or_none()
    return jsonable_encoder({**snapshot, "tenant": {"id": str(identity.tenant_id), "name": tenant_name}})
