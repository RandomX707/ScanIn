from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator
from uuid import UUID

from sqlalchemy import Connection

from packages.db.tenant import tenant_transaction


@dataclass(frozen=True)
class ValidatedJobContext:
    """Must be constructed by the trusted job-envelope validator in the worker layer."""
    tenant_id: UUID
    document_tenant_id: UUID

    def __post_init__(self) -> None:
        if self.tenant_id != self.document_tenant_id:
            raise PermissionError("job tenant does not match document tenant")


@contextmanager
def worker_tenant_transaction(connection: Connection, job: ValidatedJobContext) -> Iterator[Connection]:
    with tenant_transaction(connection, job.tenant_id) as scoped:
        yield scoped
