from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from packages.db.tenant import tenant_transaction

TENANT_DATA_TABLES = (
    "tenants", "users", "files", "documents", "document_versions", "audit_events",
    "fields", "data_corrections", "edit_sets", "patches", "reviews", "verification_manifests",
)


def _assert_test_cleanup_foreign_keys(connection) -> None:
    rows = connection.execute(text("""SELECT child.relname,parent.relname,
        ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum,ord)
              JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum ORDER BY k.ord),
        ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum,ord)
              JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=k.attnum ORDER BY k.ord)
        FROM pg_constraint c
        JOIN pg_class child ON child.oid=c.conrelid
        JOIN pg_namespace child_ns ON child_ns.oid=child.relnamespace
        JOIN pg_class parent ON parent.oid=c.confrelid
        JOIN pg_namespace parent_ns ON parent_ns.oid=parent.relnamespace
        WHERE c.contype='f' AND child_ns.nspname='public' AND parent_ns.nspname='public'
          AND child.relname::text = ANY(CAST(:tables AS text[]))
          AND parent.relname::text = ANY(CAST(:tables AS text[]))"""),
        {"tables": list(TENANT_DATA_TABLES)}).all()
    edges = {(child, parent, tuple(child_cols), tuple(parent_cols))
             for child, parent, child_cols, parent_cols in rows}
    required = {
        ("documents", "document_versions", ("tenant_id", "current_version_id"), ("tenant_id", "id")),
        ("document_versions", "documents", ("tenant_id", "document_id"), ("tenant_id", "id")),
        ("document_versions", "document_versions", ("tenant_id", "parent_version_id"), ("tenant_id", "id")),
        ("document_versions", "files", ("tenant_id", "file_id"), ("tenant_id", "id")),
        ("document_versions", "edit_sets", ("tenant_id", "edit_set_id"), ("tenant_id", "id")),
        ("edit_sets", "documents", ("tenant_id", "document_id"), ("tenant_id", "id")),
        ("edit_sets", "document_versions", ("tenant_id", "base_version_id"), ("tenant_id", "id")),
        ("edit_sets", "document_versions", ("tenant_id", "released_version_id"), ("tenant_id", "id")),
        ("fields", "documents", ("tenant_id", "document_id"), ("tenant_id", "id")),
        ("fields", "document_versions", ("tenant_id", "version_id"), ("tenant_id", "id")),
        ("data_corrections", "fields", ("tenant_id", "field_id"), ("tenant_id", "id")),
        ("data_corrections", "document_versions", ("tenant_id", "source_version_id"), ("tenant_id", "id")),
        ("patches", "edit_sets", ("tenant_id", "edit_set_id"), ("tenant_id", "id")),
        ("reviews", "edit_sets", ("tenant_id", "edit_set_id"), ("tenant_id", "id")),
        ("verification_manifests", "edit_sets", ("tenant_id", "edit_set_id"), ("tenant_id", "id")),
    }
    if not required <= edges:
        raise RuntimeError(f"test cleanup schema foreign-key contract mismatch: missing {required - edges}")


def _postgres_urls() -> tuple[str, str] | None:
    owner = os.environ.get("IDW_TEST_MIGRATION_DATABASE_URL")
    app = os.environ.get("IDW_TEST_APPLICATION_DATABASE_URL")
    return (owner, app) if owner and app else None


@pytest.fixture(scope="session")
def postgres_engines() -> tuple[Engine, Engine]:
    urls = _postgres_urls()
    if urls is None:
        pytest.skip("NOT_VERIFIED: requires PostgreSQL 16 with separate migration and application credentials")
    owner = create_engine(urls[0], pool_pre_ping=True)
    app = create_engine(urls[1], pool_size=2, max_overflow=0, pool_pre_ping=True)
    try:
        with owner.connect() as connection:
            version = int(connection.execute(text("SHOW server_version_num")).scalar_one())
            if version < 160000 or version >= 170000:
                pytest.skip(f"requires PostgreSQL 16; server_version_num={version}")
        yield owner, app
    finally:
        owner.dispose()
        app.dispose()


@pytest.fixture(scope="session")
def worker_engine() -> Engine:
    url = os.environ.get("IDW_TEST_WORKER_DATABASE_URL")
    if not url:
        pytest.skip("NOT_VERIFIED: requires the separate least-privilege worker credential")
    engine = create_engine(url, pool_size=2, max_overflow=0, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            attributes = connection.execute(text("SELECT rolsuper,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user")).one()
            if tuple(attributes) != (False, False, False):
                pytest.fail("worker role must be non-superuser, non-creator and NOBYPASSRLS")
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def tenant_pair(postgres_engines: tuple[Engine, Engine]) -> tuple[UUID, UUID]:
    owner, _ = postgres_engines
    first, second = uuid4(), uuid4()
    with owner.begin() as connection:
        for tenant_id, name in ((first, "tenant-a"), (second, "tenant-b")):
            connection.execute(text("SELECT set_config('app.tenant_id',:t,true)"), {"t": str(tenant_id)})
            connection.execute(text("INSERT INTO tenants(id,name) VALUES (:id,:name)"), {"id": tenant_id, "name": name})
    yield first, second
    with owner.begin() as connection:
        database, current_user, database_owner = connection.execute(text("""SELECT current_database(), current_user,
            pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()""")).one()
        if database != "channel360_test" or current_user != database_owner:
            raise RuntimeError("test cleanup trigger changes require the channel360_test database owner")
        _assert_test_cleanup_foreign_keys(connection)
        protected_triggers = (
            ("audit_events", "audit_no_update_delete"),
            ("data_corrections", "corrections_append_only"),
            ("reviews", "reviews_append_only"),
            ("verification_manifests", "manifests_append_only"),
            ("patches", "patches_draft_guard"),
        )
        current_trigger_states = connection.execute(text("""SELECT t.tgname::text,t.tgenabled
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relname::text = ANY(CAST(:tables AS text[])) AND NOT t.tgisinternal"""),
            {"tables": list(TENANT_DATA_TABLES)}).all()
        trigger_names = {name for name, _ in current_trigger_states}
        required_trigger_names = {trigger for _, trigger in protected_triggers}
        if not required_trigger_names <= trigger_names or any(state != "O" for _, state in current_trigger_states):
            raise RuntimeError("test cleanup requires every application trigger to exist and be enabled")
        for table, trigger in protected_triggers:
            connection.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}"))
        for tenant_id in (first, second):
            connection.execute(text("SELECT set_config('app.tenant_id',:t,true)"), {"t": str(tenant_id)})
            connection.execute(text("DELETE FROM verification_manifests WHERE tenant_id=:t"), {"t": tenant_id})
            connection.execute(text("DELETE FROM reviews WHERE tenant_id=:t"), {"t": tenant_id})
            connection.execute(text("DELETE FROM patches WHERE tenant_id=:t"), {"t": tenant_id})
            connection.execute(text("DELETE FROM data_corrections WHERE tenant_id=:t"), {"t": tenant_id})
            connection.execute(text("DELETE FROM fields WHERE tenant_id=:t"), {"t": tenant_id})
            connection.execute(text("DELETE FROM audit_events WHERE tenant_id=:t"), {"t": tenant_id})
            # Break only test-row links in both directions before deleting an
            # approved edit set with a derived version. The FK constraints
            # remain enabled throughout cleanup.
            connection.execute(text("UPDATE documents SET current_version_id=NULL WHERE tenant_id=:t"),
                               {"t": tenant_id})
            connection.execute(text("UPDATE document_versions SET edit_set_id=NULL,parent_version_id=NULL WHERE tenant_id=:t"),
                               {"t": tenant_id})
            connection.execute(text("DELETE FROM edit_sets WHERE tenant_id=:t"), {"t": tenant_id})
            connection.execute(text("DELETE FROM documents WHERE tenant_id=:t"), {"t": tenant_id})
            # document_versions_document_fk cascades versions once the document
            # current pointer and inter-version links have been cleared.
            connection.execute(text("DELETE FROM files WHERE tenant_id=:t"), {"t": tenant_id})
            connection.execute(text("DELETE FROM users WHERE tenant_id=:t"), {"t": tenant_id})
            connection.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": tenant_id})
        for table, trigger in protected_triggers:
            connection.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}"))
    with owner.connect() as connection:
        disabled = connection.execute(text("""SELECT count(*) FROM pg_trigger t
            JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relname::text = ANY(CAST(:tables AS text[]))
              AND NOT t.tgisinternal AND t.tgenabled <> 'O'"""),
            {"tables": list(TENANT_DATA_TABLES)}).scalar_one()
        if disabled:
            raise RuntimeError(f"test cleanup left {disabled} application triggers disabled")
    with owner.connect() as connection:
        for tenant_id in (first, second):
            with tenant_transaction(connection, tenant_id):
                for table in TENANT_DATA_TABLES[1:]:
                    count = connection.execute(text(f"SELECT count(*) FROM {table} WHERE tenant_id=:t"),
                                               {"t": tenant_id}).scalar_one()
                    if count:
                        raise RuntimeError(f"test cleanup left {count} rows in {table} for tenant {tenant_id}")
                count = connection.execute(text("SELECT count(*) FROM tenants WHERE id=:t"),
                                          {"t": tenant_id}).scalar_one()
                if count:
                    raise RuntimeError(f"test cleanup left tenant row {tenant_id}")
