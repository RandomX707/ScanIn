from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Connection

from packages.api.auth import AuthenticatedIdentity
from packages.db.tenant import tenant_transaction


@contextmanager
def api_tenant_transaction(connection: Connection, identity: AuthenticatedIdentity) -> Iterator[Connection]:
    """Set RLS context from an identity already validated by require_identity."""
    with tenant_transaction(connection, identity.tenant_id) as scoped:
        yield scoped
