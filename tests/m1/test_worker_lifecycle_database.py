from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from packages.db.tenant import tenant_transaction
from packages.db.worker_context import ValidatedJobContext, worker_tenant_transaction
from packages.workers.lifecycle import WorkerLifecycleRepository

pytestmark = pytest.mark.postgres


def test_only_isolated_worker_role_can_advance_tenant_scoped_document_state(postgres_engines, worker_engine, tenant_pair):
    _, app = postgres_engines
    tenant, _ = tenant_pair
    document_id = uuid4()
    with app.connect() as connection, tenant_transaction(connection, tenant):
        connection.execute(text("INSERT INTO documents(id,tenant_id,filename) VALUES (:id,:tenant,'claim.csv')"),
                           {"id": document_id, "tenant": tenant})
    with app.connect() as connection:
        with pytest.raises(DBAPIError):
            with tenant_transaction(connection, tenant):
                connection.execute(text("UPDATE documents SET ingestion_state='scan_pending' WHERE tenant_id=:tenant AND id=:id"),
                                   {"tenant": tenant, "id": document_id})

    job = ValidatedJobContext(tenant, tenant)
    with worker_engine.connect() as connection, worker_tenant_transaction(connection, job):
        lifecycle = WorkerLifecycleRepository(connection, job)
        assert lifecycle.transition(document_id, outcome="begin", actor_id="worker-test") == "inspecting"
    with worker_engine.connect() as connection, worker_tenant_transaction(connection, job):
        assert WorkerLifecycleRepository(connection, job).transition(document_id, outcome="ready", actor_id="worker-test") == "ready"
    with app.connect() as connection, tenant_transaction(connection, tenant):
        state = connection.execute(text("SELECT ingestion_state FROM documents WHERE tenant_id=:tenant AND id=:id"),
                                   {"tenant": tenant, "id": document_id}).scalar_one()
        assert state == "ready"


def test_worker_context_cannot_claim_a_different_document_tenant(tenant_pair):
    tenant_a, tenant_b = tenant_pair
    with pytest.raises(PermissionError, match="job tenant does not match document tenant"):
        ValidatedJobContext(tenant_a, tenant_b)
