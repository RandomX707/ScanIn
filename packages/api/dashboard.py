from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder

from packages.api.auth import AuthenticatedIdentity, require_identity
from packages.api.db_context import api_tenant_transaction
from packages.registry.documents import DocumentRepository

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/summary")
def dashboard_summary(request: Request,
                      identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        snapshot = DocumentRepository(connection, identity.tenant_id).dashboard_snapshot()
    return jsonable_encoder(snapshot)
