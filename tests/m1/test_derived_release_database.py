from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import text

from packages.db.tenant import tenant_transaction
from packages.registry.documents import DocumentRepository
from packages.storage.local import StoredFile

pytestmark = pytest.mark.postgres


def test_approved_release_lineage_retry_and_fk_safe_cleanup(postgres_engines, tenant_pair, tmp_path):
    owner, app = postgres_engines
    tenant = tenant_pair[0]
    document, edit_set = uuid4(), uuid4()
    original = b"policy,value\nP-100,old\n"
    original_hash = hashlib.sha256(original).hexdigest()
    original_path = tmp_path / "original.csv"
    original_path.write_bytes(original)
    original_stored = StoredFile(tenant, document, original_path,
        f"{tenant}/originals/{document}/{original_hash}", len(original), original_hash)
    with app.connect() as connection, tenant_transaction(connection, tenant):
        original_version = DocumentRepository(connection, tenant).register_clean_original(
            filename="synthetic.csv", media_type="text/csv", stored=original_stored, actor_id="processor")
    with owner.connect() as connection, tenant_transaction(connection, tenant):
        for state in ("scan_pending", "scanning", "scan_clean", "inspecting", "ready"):
            connection.execute(text("UPDATE documents SET ingestion_state=:state WHERE tenant_id=:tenant AND id=:doc"),
                               {"state": state, "tenant": tenant, "doc": document})
    with app.connect() as connection, tenant_transaction(connection, tenant):
        repo = DocumentRepository(connection, tenant)
        repo.set_document_category(document, category="insurer-authored", actor_id="admin")
        repo.create_edit_set(edit_set_id=edit_set, document_id=document,
            base_version_id=original_version.version_id, category="insurer-authored",
            actor_id="processor", reason="integration persistence proof")
    with app.connect() as connection, tenant_transaction(connection, tenant):
        DocumentRepository(connection, tenant).freeze_edit_set(edit_set, frozen_hash="a" * 64,
            plan={"test_marker": "repository-persistence-only"}, adapter="csv", adapter_version="test", actor_id="processor")
    with app.connect() as connection, tenant_transaction(connection, tenant):
        DocumentRepository(connection, tenant).review_edit_set(edit_set, decision="approved",
            reviewer_id="reviewer", frozen_hash="a" * 64)

    derived = b"policy,value\nP-100,new\n"
    derived_hash = hashlib.sha256(derived).hexdigest()
    derived_path = tmp_path / "derived.csv"
    derived_path.write_bytes(derived)
    derived_stored = StoredFile(tenant, document, derived_path,
        f"{tenant}/derived/{document}/{edit_set}/{derived_hash}", len(derived), derived_hash)
    manifest = {"source_sha256": original_hash, "media_type": "text/csv", "outcome": "VERIFIED"}
    with app.connect() as connection, tenant_transaction(connection, tenant):
        repo = DocumentRepository(connection, tenant)
        first_version = repo.commit_derived_release(edit_set_id=edit_set, stored=derived_stored,
            outcome="VERIFIED", adapter="csv", adapter_version="test", manifest=manifest, actor_id="processor")
    with app.connect() as connection, tenant_transaction(connection, tenant):
        repo = DocumentRepository(connection, tenant)
        second_version = repo.commit_derived_release(edit_set_id=edit_set, stored=derived_stored,
            outcome="VERIFIED", adapter="csv", adapter_version="test", manifest=manifest, actor_id="processor")
        assert first_version == second_version
        assert repo.get_original(document)["sha256"].strip() == original_hash
        assert repo.get(document)["sha256"].strip() == derived_hash
        versions = repo.list_versions(document)
        assert len(versions) == 2
        assert next(item for item in versions if item["kind"] == "derived")["verification_outcome"] == "VERIFIED"
    # tenant_pair teardown then exercises FK-safe cleanup of this approved edit
    # set, its review/manifest, and the derived version linked back to the set.
