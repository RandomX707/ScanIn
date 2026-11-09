from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from packages.db.tenant import tenant_transaction

pytestmark = pytest.mark.postgres


def test_edit_sets_freeze_for_owner_release_without_creating_review_records(postgres_engines, tenant_pair):
    owner, app = postgres_engines
    tenant = tenant_pair[0]
    document_id, version_id, file_id, edit_set_id, patch_id = (uuid4() for _ in range(5))
    with owner.connect() as connection, tenant_transaction(connection, tenant):
        connection.execute(text("INSERT INTO documents(id,tenant_id,filename) VALUES (:id,:tenant,'policy.xlsx')"),
                           {"id": document_id, "tenant": tenant})
        connection.execute(text("INSERT INTO files(id,tenant_id,sha256,size_bytes,mime,storage_key,file_role) "
                                "VALUES (:id,:tenant,:sha,0,'application/vnd.xlsx','test/key','original')"),
                           {"id": file_id, "tenant": tenant, "sha": "0" * 64})
        connection.execute(text("INSERT INTO document_versions(id,tenant_id,document_id,file_id,kind,sha256) "
                                "VALUES (:id,:tenant,:doc,:file,'original',:sha)"),
                           {"id": version_id, "tenant": tenant, "doc": document_id, "file": file_id, "sha": "0" * 64})

    with app.connect() as connection:
        with pytest.raises(DBAPIError):
            with tenant_transaction(connection, tenant):
                connection.execute(text("""INSERT INTO edit_sets
                    (id,tenant_id,document_id,base_version_id,status,created_by,reason)
                    VALUES (:id,:tenant,:doc,:version,'released','processor','invalid direct release')"""),
                    {"id": edit_set_id, "tenant": tenant, "doc": document_id, "version": version_id})

    with app.connect() as connection, tenant_transaction(connection, tenant):
        connection.execute(text("""INSERT INTO edit_sets
            (id,tenant_id,document_id,base_version_id,created_by,reason)
            VALUES (:id,:tenant,:doc,:version,'channel360-demo-owner','owner correction')"""),
            {"id": edit_set_id, "tenant": tenant, "doc": document_id, "version": version_id})
        connection.execute(text("""INSERT INTO patches
            (id,tenant_id,edit_set_id,target_id,expected_old_value,expected_fingerprint,new_value)
            VALUES (:id,:tenant,:edit,'xlsx:Claim!A1',CAST(:old AS jsonb),:fingerprint,CAST(:new AS jsonb))"""),
            {"id": patch_id, "tenant": tenant, "edit": edit_set_id,
             "old": '{"type":"string","value":"old"}', "fingerprint": "1" * 64,
             "new": '{"type":"string","value":"new"}'})
        connection.execute(text("""UPDATE edit_sets SET status='ready_for_release',frozen_hash=:hash,
            patch_plan=CAST(:plan AS jsonb),adapter='xlsx',adapter_version='test'
            WHERE tenant_id=:tenant AND id=:id"""),
            {"hash": "2" * 64, "plan": '{"plan_sha256":"test"}', "tenant": tenant, "id": edit_set_id})

    with app.connect() as connection, tenant_transaction(connection, tenant):
        status, frozen_hash, reviews = connection.execute(text("""SELECT e.status,e.frozen_hash,
            (SELECT count(*) FROM reviews r WHERE r.tenant_id=e.tenant_id AND r.edit_set_id=e.id)
            FROM edit_sets e WHERE e.tenant_id=:tenant AND e.id=:edit"""),
            {"tenant": tenant, "edit": edit_set_id}).one()
        assert status == "ready_for_release"
        assert frozen_hash.strip() == "2" * 64
        assert reviews == 0

    # The owner-release state machine has no transition that creates an approval.
    with app.connect() as connection:
        with pytest.raises(DBAPIError):
            with tenant_transaction(connection, tenant):
                connection.execute(text("UPDATE edit_sets SET status='approved' WHERE tenant_id=:tenant AND id=:id"),
                                   {"tenant": tenant, "id": edit_set_id})

    with app.connect() as connection:
        with pytest.raises(DBAPIError):
            with tenant_transaction(connection, tenant):
                connection.execute(text("UPDATE patches SET new_value=CAST(:value AS jsonb) WHERE tenant_id=:tenant AND id=:id"),
                                   {"value": '{"type":"string","value":"tampered"}',
                                    "tenant": tenant, "id": patch_id})
