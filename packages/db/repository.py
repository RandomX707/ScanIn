from __future__ import annotations

from uuid import UUID

from sqlalchemy import Connection, text

from packages.db.tenant import require_tenant_context


class TenantRepository:
    """Small tenant-filtered repository; PostgreSQL RLS remains the second boundary."""

    def __init__(self, connection: Connection, tenant_id: UUID):
        active_tenant = require_tenant_context(connection)
        if active_tenant != tenant_id:
            raise PermissionError("repository tenant does not match transaction context")
        self._connection = connection
        self._tenant_id = tenant_id

    def get_document(self, document_id: UUID) -> dict[str, object] | None:
        row = self._connection.execute(
            text("SELECT id, tenant_id, filename, ingestion_state FROM documents WHERE tenant_id=:tenant AND id=:id"),
            {"tenant": self._tenant_id, "id": document_id},
        ).mappings().one_or_none()
        return dict(row) if row is not None else None

    def insert_document(self, document_id: UUID, filename: str) -> None:
        self._connection.execute(
            text("INSERT INTO documents (id, tenant_id, filename, ingestion_state) VALUES (:id,:tenant,:filename,'staged_private')"),
            {"id": document_id, "tenant": self._tenant_id, "filename": filename},
        )
