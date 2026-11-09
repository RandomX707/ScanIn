"""Phase 3B (N7): stale edit sets are rebased into a NEW draft; the predecessor is superseded, never rewritten."""
from __future__ import annotations

import threading
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from packages.db.tenant import tenant_transaction
from packages.db.worker_context import ValidatedJobContext, worker_tenant_transaction
from packages.registry.documents import DocumentRepository
from packages.workers.lifecycle import WorkerLifecycleRepository

pytestmark = pytest.mark.postgres
OWNER = "channel360-demo-owner"


def _stale_frozen_edit_set(owner, app, worker_engine, tenant):
    """A document whose current version (v2) moved on after edit set A was frozen on v1."""
    document_id, v1, v2, f1, f2, edit_a = (uuid4() for _ in range(6))
    with owner.connect() as connection, tenant_transaction(connection, tenant):
        connection.execute(text("INSERT INTO documents(id,tenant_id,filename,current_version_id) VALUES (:id,:t,'claims.csv',:v)"),
                           {"id": document_id, "t": tenant, "v": v1})
        for file_id, version_id, parent, kind in ((f1, v1, None, "original"), (f2, v2, v1, "derived")):
            connection.execute(text("INSERT INTO files(id,tenant_id,sha256,size_bytes,mime,storage_key,file_role) "
                                    "VALUES (:id,:t,:sha,1,'text/csv',:key,'original')"),
                               {"id": file_id, "t": tenant, "sha": str(kind[0]) * 64, "key": f"test/{file_id}"})
            connection.execute(text("INSERT INTO document_versions(id,tenant_id,document_id,parent_version_id,file_id,kind,sha256) "
                                    "VALUES (:id,:t,:doc,:parent,:file,'original',:sha)"),
                               {"id": version_id, "t": tenant, "doc": document_id, "parent": parent, "file": file_id,
                                "sha": str(kind[0]) * 64})
    job = ValidatedJobContext(tenant, tenant)
    for outcome in ("begin", "ready"):
        with worker_engine.connect() as connection, worker_tenant_transaction(connection, job):
            WorkerLifecycleRepository(connection, job).transition(document_id, actor_id="worker", outcome=outcome)
    with app.connect() as connection, tenant_transaction(connection, tenant):
        repo = DocumentRepository(connection, tenant)
        repo.set_document_category(document_id, category="insurer-authored", actor_id=OWNER)
        repo.create_edit_set(edit_set_id=edit_a, document_id=document_id, base_version_id=v1,
                             category="insurer-authored", actor_id=OWNER, reason="Fix claimant name")
        repo.add_patches(edit_a, [{"target_id": "csv:2:2", "expected_old_value": {"type": "string", "value": "Asha"},
                                   "expected_fingerprint": "a" * 64, "new_value": {"type": "string", "value": "Asha Rao"}}],
                         actor_id=OWNER)
        repo.freeze_edit_set(edit_a, frozen_hash="f" * 64, plan={"plan_sha256": "p" * 64}, adapter="csv",
                             adapter_version="0.1.0", actor_id=OWNER)
    with owner.connect() as connection, tenant_transaction(connection, tenant):
        connection.execute(text("UPDATE documents SET current_version_id=:v WHERE tenant_id=:t AND id=:id"),
                           {"v": v2, "t": tenant, "id": document_id})
    return document_id, v1, v2, edit_a


SUCCESSOR_PATCH = [{"target_id": "csv:2:2", "expected_old_value": {"type": "string", "value": "Asha"},
                    "expected_fingerprint": "b" * 64, "new_value": {"type": "string", "value": "Asha Rao"}}]


def _row(connection, tenant, edit_set_id):
    return connection.execute(text("""SELECT status,frozen_hash,patch_plan,base_version_id,supersedes,superseded_by
        FROM edit_sets WHERE tenant_id=:t AND id=:id"""), {"t": tenant, "id": edit_set_id}).mappings().one()


def test_frozen_stale_edit_set_is_superseded_by_a_new_draft_without_rewriting_history(postgres_engines, worker_engine, tenant_pair):
    owner, app = postgres_engines
    tenant, other = tenant_pair
    document_id, v1, v2, edit_a = _stale_frozen_edit_set(owner, app, worker_engine, tenant)
    successor = uuid4()
    with app.connect() as connection, tenant_transaction(connection, tenant):
        repo = DocumentRepository(connection, tenant)
        patches_before = repo.list_patches(edit_a)
        outcome = repo.supersede_with_rebased_draft(predecessor_id=edit_a, successor_id=successor, document_id=document_id,
                                                    new_version_id=v2, actor_id=OWNER, patches=SUCCESSOR_PATCH)
        assert outcome["previous_status"] == "ready_for_release" and outcome["frozen_hash"] == "f" * 64
    with app.connect() as connection, tenant_transaction(connection, tenant):
        repo = DocumentRepository(connection, tenant)
        old, new = _row(connection, tenant, edit_a), _row(connection, tenant, successor)
        assert (old["status"], old["superseded_by"], old["frozen_hash"].strip(), old["base_version_id"]) == ("superseded", successor, "f" * 64, v1)
        assert old["patch_plan"] == {"plan_sha256": "p" * 64}
        assert (new["status"], new["supersedes"], new["base_version_id"], new["frozen_hash"]) == ("draft", edit_a, v2, None)
        assert repo.list_patches(edit_a) == patches_before
        assert repo.list_patches(successor)[0]["expected_fingerprint"].strip() == "b" * 64
        events = repo.list_document_audit(document_id, limit=50)
        superseded = next(e for e in events if e["action"] == "edit_set.superseded")
        assert superseded["subject"] == str(edit_a)
        assert superseded["payload"]["successor_edit_set_id"] == str(successor)
        assert superseded["payload"]["frozen_hash"] == "f" * 64
        created = next(e for e in events if e["action"] == "edit_set.draft_created" and e["subject"] == str(successor))
        assert created["payload"]["supersedes"] == str(edit_a)
    with app.connect() as connection, tenant_transaction(connection, other):
        assert DocumentRepository(connection, other).get_edit_set(edit_a) is None


def test_superseded_edit_set_cannot_be_released_modified_or_rebased_again(postgres_engines, worker_engine, tenant_pair):
    owner, app = postgres_engines
    tenant = tenant_pair[0]
    document_id, v1, v2, edit_a = _stale_frozen_edit_set(owner, app, worker_engine, tenant)
    with app.connect() as connection, tenant_transaction(connection, tenant):
        DocumentRepository(connection, tenant).supersede_with_rebased_draft(
            predecessor_id=edit_a, successor_id=uuid4(), document_id=document_id, new_version_id=v2,
            actor_id=OWNER, patches=SUCCESSOR_PATCH)
    with app.connect() as connection, tenant_transaction(connection, tenant):
        with pytest.raises(ValueError, match="edit_set_not_rebasable"):
            DocumentRepository(connection, tenant).supersede_with_rebased_draft(
                predecessor_id=edit_a, successor_id=uuid4(), document_id=document_id, new_version_id=v2,
                actor_id=OWNER, patches=SUCCESSOR_PATCH)
    for statement in ("UPDATE edit_sets SET status='released' WHERE tenant_id=:t AND id=:id",
                      "UPDATE edit_sets SET status='ready_for_release' WHERE tenant_id=:t AND id=:id",
                      "UPDATE edit_sets SET superseded_by=NULL,status='draft' WHERE tenant_id=:t AND id=:id",
                      "UPDATE edit_sets SET frozen_hash=repeat('0',64) WHERE tenant_id=:t AND id=:id",
                      """INSERT INTO patches(tenant_id,edit_set_id,target_id,expected_old_value,expected_fingerprint,new_value)
                         VALUES (:t,:id,'csv:3:3','{"type":"string","value":"x"}',repeat('c',64),'{"type":"string","value":"y"}')""",
                      "UPDATE patches SET expected_fingerprint=repeat('d',64) WHERE tenant_id=:t AND edit_set_id=:id"):
        with app.connect() as connection:
            with pytest.raises(DBAPIError):
                with tenant_transaction(connection, tenant):
                    connection.execute(text(statement), {"t": tenant, "id": edit_a})
    with app.connect() as connection, tenant_transaction(connection, tenant):
        with pytest.raises(ValueError, match="edit_set_not_ready_for_release"):
            from packages.storage.local import StoredFile
            DocumentRepository(connection, tenant).commit_derived_release(
                edit_set_id=edit_a, stored=StoredFile(tenant, document_id, "/dev/null", "k", 1, "e" * 64),
                outcome="VERIFIED", adapter="csv", adapter_version="0.1.0",
                manifest={"source_sha256": "o" * 64, "outcome": "VERIFIED", "checks": []}, actor_id=OWNER)


def test_supersession_requires_a_successor_and_one_successor_only(postgres_engines, worker_engine, tenant_pair):
    owner, app = postgres_engines
    tenant = tenant_pair[0]
    document_id, v1, v2, edit_a = _stale_frozen_edit_set(owner, app, worker_engine, tenant)
    with app.connect() as connection:
        with pytest.raises(DBAPIError):
            with tenant_transaction(connection, tenant):
                connection.execute(text("UPDATE edit_sets SET status='superseded' WHERE tenant_id=:t AND id=:id"),
                                   {"t": tenant, "id": edit_a})
    first, second = uuid4(), uuid4()
    with app.connect() as connection, tenant_transaction(connection, tenant):
        connection.execute(text("""INSERT INTO edit_sets(id,tenant_id,document_id,base_version_id,authorized_category,created_by,reason,supersedes)
            VALUES (:id,:t,:doc,:v,'insurer-authored',:o,'successor',:a)"""), {"id": first, "t": tenant, "doc": document_id, "v": v2, "o": OWNER, "a": edit_a})
    with app.connect() as connection:
        with pytest.raises(DBAPIError):
            with tenant_transaction(connection, tenant):
                connection.execute(text("""INSERT INTO edit_sets(id,tenant_id,document_id,base_version_id,authorized_category,created_by,reason,supersedes)
                    VALUES (:id,:t,:doc,:v,'insurer-authored',:o,'competing successor',:a)"""),
                    {"id": second, "t": tenant, "doc": document_id, "v": v2, "o": OWNER, "a": edit_a})


def test_concurrent_rebases_create_exactly_one_successor(postgres_engines, worker_engine, tenant_pair):
    owner, app = postgres_engines
    tenant = tenant_pair[0]
    document_id, v1, v2, edit_a = _stale_frozen_edit_set(owner, app, worker_engine, tenant)
    barrier = threading.Barrier(2)
    results: list[object] = []

    def attempt():
        successor = uuid4()
        try:
            with app.connect() as connection, tenant_transaction(connection, tenant):
                barrier.wait(timeout=10)
                DocumentRepository(connection, tenant).supersede_with_rebased_draft(
                    predecessor_id=edit_a, successor_id=successor, document_id=document_id, new_version_id=v2,
                    actor_id=OWNER, patches=SUCCESSOR_PATCH)
            results.append(("ok", successor))
        except (ValueError, DBAPIError) as error:
            results.append(("refused", str(error).split("\n")[0][:80]))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    [thread.start() for thread in threads]
    [thread.join(timeout=30) for thread in threads]
    assert sorted(kind for kind, _ in results) == ["ok", "refused"], results
    with app.connect() as connection, tenant_transaction(connection, tenant):
        successors = connection.execute(text("SELECT id FROM edit_sets WHERE tenant_id=:t AND supersedes=:a"),
                                        {"t": tenant, "a": edit_a}).scalars().all()
        winner = next(value for kind, value in results if kind == "ok")
        assert successors == [winner] and _row(connection, tenant, edit_a)["superseded_by"] == winner


def test_rebase_refuses_wrong_author_or_moving_version(postgres_engines, worker_engine, tenant_pair):
    owner, app = postgres_engines
    tenant = tenant_pair[0]
    document_id, v1, v2, edit_a = _stale_frozen_edit_set(owner, app, worker_engine, tenant)
    with app.connect() as connection, tenant_transaction(connection, tenant):
        repo = DocumentRepository(connection, tenant)
        with pytest.raises(ValueError, match="edit_set_not_rebasable"):
            repo.supersede_with_rebased_draft(predecessor_id=edit_a, successor_id=uuid4(), document_id=document_id,
                                              new_version_id=v2, actor_id="someone-else", patches=SUCCESSOR_PATCH)
        with pytest.raises(ValueError, match="stale_base_version"):
            repo.supersede_with_rebased_draft(predecessor_id=edit_a, successor_id=uuid4(), document_id=document_id,
                                              new_version_id=uuid4(), actor_id=OWNER, patches=SUCCESSOR_PATCH)
    with app.connect() as connection, tenant_transaction(connection, tenant):
        assert _row(connection, tenant, edit_a)["status"] == "ready_for_release"
        assert connection.execute(text("SELECT count(*) FROM edit_sets WHERE tenant_id=:t AND supersedes=:a"),
                                  {"t": tenant, "a": edit_a}).scalar_one() == 0
