from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from packages.db.tenant import tenant_transaction
from packages.db.worker_context import ValidatedJobContext, worker_tenant_transaction
from packages.registry.documents import DocumentRepository
from packages.workers.lifecycle import WorkerLifecycleRepository

pytestmark = pytest.mark.postgres


def test_manual_field_and_correction_are_tenant_scoped_audited_and_leave_source_immutable(postgres_engines, worker_engine, tenant_pair):
    _, app = postgres_engines
    tenant, other_tenant = tenant_pair
    document_id, version_id, file_id = uuid4(), uuid4(), uuid4()
    source_sha = "a" * 64

    with app.connect() as connection, tenant_transaction(connection, tenant):
        connection.execute(text("""INSERT INTO documents
            (id,tenant_id,filename,ingestion_state,current_version_id)
            VALUES (:id,:tenant,'claim.csv','staged_private',:version)"""),
            {"id": document_id, "tenant": tenant, "version": version_id})
        connection.execute(text("""INSERT INTO files
            (id,tenant_id,sha256,size_bytes,mime,storage_key,file_role)
            VALUES (:id,:tenant,:sha,10,'text/csv','test/original','original')"""),
            {"id": file_id, "tenant": tenant, "sha": source_sha})
        connection.execute(text("""INSERT INTO document_versions
            (id,tenant_id,document_id,file_id,kind,sha256)
            VALUES (:id,:tenant,:document,:file,'original',:sha)"""),
            {"id": version_id, "tenant": tenant, "document": document_id, "file": file_id, "sha": source_sha})
    job = ValidatedJobContext(tenant, tenant)
    with worker_engine.connect() as connection, worker_tenant_transaction(connection, job):
        lifecycle = WorkerLifecycleRepository(connection, job)
        assert lifecycle.transition(document_id, actor_id="worker-test", outcome="begin") == "inspecting"
    with worker_engine.connect() as connection, worker_tenant_transaction(connection, job):
        lifecycle = WorkerLifecycleRepository(connection, job)
        assert lifecycle.transition(document_id, actor_id="worker-test", outcome="ready") == "ready"
    with app.connect() as connection, tenant_transaction(connection, tenant):
        repo = DocumentRepository(connection, tenant)
        field_id = repo.create_manual_field(document_id=document_id, version_id=version_id,
            target_id="csv:2:1", fingerprint="b" * 64, label="Claim number",
            observed_value={"type": "string", "value": "CLM-1"}, actor_id="processor-a")
        correction_id = repo.append_data_correction(document_id=document_id, field_id=field_id,
            value={"type": "string", "value": "CLM-2"}, reason="Matched source paperwork", actor_id="processor-a")
        fields = repo.list_fields(document_id)
        corrections = repo.list_data_corrections(document_id)
        assert len(fields) == len(corrections) == 1
        assert corrections[0]["corrected_value"] == {"type": "string", "value": "CLM-2"}
        assert corrections[0]["id"] == correction_id
        original = connection.execute(text("SELECT sha256 FROM document_versions WHERE tenant_id=:tenant AND id=:version"),
                                      {"tenant": tenant, "version": version_id}).scalar_one()
        assert original == source_sha

    with app.connect() as connection, tenant_transaction(connection, other_tenant):
        assert DocumentRepository(connection, other_tenant).list_fields(document_id) == []
        assert DocumentRepository(connection, other_tenant).list_data_corrections(document_id) == []


def test_missing_tenant_context_cannot_read_manual_fields(postgres_engines):
    _, app = postgres_engines
    with app.connect() as connection:
        visible = connection.execute(text("SELECT count(*) FROM fields")).scalar_one()
        assert visible == 0


def test_text_anchor_field_keeps_evidence_and_correction_provenance_without_new_versions(postgres_engines, worker_engine, tenant_pair):
    _, app = postgres_engines
    tenant, other_tenant = tenant_pair
    document_id, version_id, file_id = uuid4(), uuid4(), uuid4()
    source_sha = "c" * 64
    with app.connect() as connection, tenant_transaction(connection, tenant):
        connection.execute(text("""INSERT INTO documents (id,tenant_id,filename,ingestion_state,current_version_id)
            VALUES (:id,:tenant,'invoice.pdf','staged_private',:version)"""),
            {"id": document_id, "tenant": tenant, "version": version_id})
        connection.execute(text("""INSERT INTO files (id,tenant_id,sha256,size_bytes,mime,storage_key,file_role)
            VALUES (:id,:tenant,:sha,10,'application/pdf','test/original-pdf','original')"""),
            {"id": file_id, "tenant": tenant, "sha": source_sha})
        connection.execute(text("""INSERT INTO document_versions (id,tenant_id,document_id,file_id,kind,sha256)
            VALUES (:id,:tenant,:document,:file,'original',:sha)"""),
            {"id": version_id, "tenant": tenant, "document": document_id, "file": file_id, "sha": source_sha})
    job = ValidatedJobContext(tenant, tenant)
    for outcome in ("begin", "ready"):
        with worker_engine.connect() as connection, worker_tenant_transaction(connection, job):
            WorkerLifecycleRepository(connection, job).transition(document_id, actor_id="worker-test", outcome=outcome)
    evidence = [{"kind": "text_selection", "source_sha256": source_sha, "source_version_id": str(version_id),
                 "location": {"page": 1}, "start": 31, "end": 43, "context_before": "Insured: "}]
    with app.connect() as connection, tenant_transaction(connection, tenant):
        repo = DocumentRepository(connection, tenant)
        versions_before = repo.list_versions(document_id)
        field_id = repo.create_manual_field(document_id=document_id, version_id=version_id,
            target_id="text:pdf:page:1:31-43", fingerprint="d" * 64, label="Insured name",
            observed_value={"type": "string", "value": "Rahul Sharma"}, actor_id="owner", evidence=evidence)
        repo.append_data_correction(document_id=document_id, field_id=field_id,
            value={"type": "string", "value": "Rahul K. Sharma"}, reason="Matches the signed proposal", actor_id="owner")
        field = repo.list_fields(document_id)[0]
        correction = repo.list_data_corrections(document_id)[0]
        assert field["evidence"] == evidence and field["target_id"] == "text:pdf:page:1:31-43"
        assert correction["source_sha256"] == source_sha and correction["source_version_id"] == version_id
        assert correction["target_fingerprint"] == "d" * 64 and correction["evidence"] == evidence
        assert correction["observed_value"]["value"] == "Rahul Sharma"
        assert correction["corrected_value"]["value"] == "Rahul K. Sharma"
        assert repo.list_versions(document_id) == versions_before
        created = [event for event in repo.list_document_audit(document_id, limit=50)
                   if event["action"] == "field.manual_created"]
        assert created and created[-1]["payload"]["anchor_kind"] == "text_selection"
    with app.connect() as connection, tenant_transaction(connection, other_tenant):
        assert DocumentRepository(connection, other_tenant).list_data_corrections(document_id) == []
