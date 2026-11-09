from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from packages.audit.chain import verify_chain
from packages.db.tenant import tenant_transaction
from packages.registry.documents import DocumentRepository
from packages.storage.local import StoredFile

pytestmark = pytest.mark.postgres


def test_original_file_document_version_and_audit_commit_atomically(postgres_engines, tenant_pair, tmp_path):
    _, app = postgres_engines
    tenant = tenant_pair[0]
    document = uuid4()
    payload = b"only a cleanly scanned source is registered"
    digest = hashlib.sha256(payload).hexdigest()
    source = tmp_path / "source"
    source.write_bytes(payload)
    stored = StoredFile(tenant, document, source, f"{tenant}/originals/{document}/{digest}", len(payload), digest)

    with app.connect() as connection, tenant_transaction(connection, tenant):
        repo = DocumentRepository(connection, tenant)
        registered = repo.register_clean_original(filename="sample.xlsx", media_type="application/zip",
                                                   stored=stored, actor_id="test-user")
        loaded = repo.get(document)
        assert loaded is not None
        assert loaded["current_version_id"] == registered.version_id
        assert loaded["sha256"].strip() == digest
        assert loaded["ingestion_state"] == "staged_private"
        assert repo.get_original(document)["version_id"] == registered.version_id
        assert repo.list_recent(limit=10)[0]["id"] == document
        assert verify_chain(connection, tenant) == (True, None)


def test_repository_refuses_stored_file_from_another_tenant(postgres_engines, tenant_pair, tmp_path):
    _, app = postgres_engines
    tenant_a, tenant_b = tenant_pair
    payload = b"tenant boundary"
    source = tmp_path / "source"
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    stored = StoredFile(tenant_b, uuid4(), source, f"{tenant_b}/originals/x/{digest}", len(payload), digest)
    with app.connect() as connection, tenant_transaction(connection, tenant_a):
        with pytest.raises(PermissionError, match="stored file tenant"):
            DocumentRepository(connection, tenant_a).register_clean_original(
                filename="x.xlsx", media_type="application/zip", stored=stored, actor_id="test-user"
            )


def test_document_audit_history_query_returns_document_events(postgres_engines, tenant_pair, tmp_path):
    _, app = postgres_engines
    tenant = tenant_pair[0]
    document = uuid4()
    payload = b"synthetic audit query fixture"
    digest = hashlib.sha256(payload).hexdigest()
    source = tmp_path / "audit-source"
    source.write_bytes(payload)
    stored = StoredFile(tenant, document, source, f"{tenant}/originals/{document}/{digest}", len(payload), digest)

    with app.connect() as connection, tenant_transaction(connection, tenant):
        repo = DocumentRepository(connection, tenant)
        repo.register_clean_original(filename="audit.csv", media_type="text/csv", stored=stored, actor_id="test-user")
        events = repo.list_document_audit(document)

    assert len(events) == 1
    assert events[0]["action"] == "document.original_registered"


def test_dashboard_snapshot_is_tenant_scoped_and_uses_registered_records(postgres_engines, tenant_pair, tmp_path):
    _, app = postgres_engines
    tenant = tenant_pair[0]
    document = uuid4()
    payload = b"synthetic dashboard fixture"
    digest = hashlib.sha256(payload).hexdigest()
    source = tmp_path / "dashboard-source"
    source.write_bytes(payload)
    stored = StoredFile(tenant, document, source, f"{tenant}/originals/{document}/{digest}", len(payload), digest)

    with app.connect() as connection, tenant_transaction(connection, tenant):
        repo = DocumentRepository(connection, tenant)
        repo.register_clean_original(filename="dashboard.csv", media_type="text/csv", stored=stored, actor_id="dashboard-test")
        snapshot = repo.dashboard_snapshot()

    assert snapshot["metrics"]["total_documents"] == 1
    assert snapshot["metrics"]["awaiting_inspection"] == 1
    assert snapshot["metrics"]["documents_with_corrections"] == 0
    assert snapshot["metrics"]["ready_for_release"] == 0
    assert snapshot["metrics"]["verified_versions"] == 0
    assert snapshot["recent_documents"][0]["filename"] == "dashboard.csv"
    assert snapshot["recent_activity"][0]["action"] == "document.original_registered"
