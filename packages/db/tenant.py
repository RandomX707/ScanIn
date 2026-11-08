from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator
from uuid import UUID

from sqlalchemy import Connection, text


@contextmanager
def tenant_transaction(connection: Connection, tenant_id: UUID) -> Iterator[Connection]:
    """Open a transaction whose tenant context is transaction-local.

    Callers must supply tenant_id from a validated identity or a validated job envelope.
    The function never accepts a tenant header or mutable request parameter implicitly.
    """
    if connection.in_transaction():
        raise RuntimeError("tenant_transaction_requires_idle_connection")
    with connection.begin():
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
        yield connection


def require_tenant_context(connection: Connection) -> UUID:
    value = connection.execute(text("SELECT nullif(current_setting('app.tenant_id', true), '')")).scalar_one_or_none()
    if value is None:
        raise RuntimeError("authenticated tenant context is required")
    return UUID(value)
