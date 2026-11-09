from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from packages.db.repository import TenantRepository
from packages.db.tenant import tenant_transaction

pytestmark = pytest.mark.postgres


def test_tenant_a_cannot_read_or_insert_tenant_b_rows(postgres_engines, tenant_pair):
    _, app = postgres_engines
    a, b = tenant_pair
    doc_b = uuid4()
    with app.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(b)})
        connection.execute(text("INSERT INTO documents(id,tenant_id,filename) VALUES (:id,:t,'private')"),
                           {"id": doc_b, "t": b})
    with app.connect() as connection, tenant_transaction(connection, a):
        assert TenantRepository(connection, a).get_document(doc_b) is None
    with app.connect() as connection:
        with pytest.raises(DBAPIError):
            with tenant_transaction(connection, a):
                connection.execute(text("INSERT INTO documents(id,tenant_id,filename) VALUES (:id,:t,'forged')"),
                                   {"id": uuid4(), "t": b})


def test_untrusted_document_cannot_start_in_a_visible_state(postgres_engines, tenant_pair):
    _, app = postgres_engines
    tenant = tenant_pair[0]
    with app.connect() as connection:
        with pytest.raises(DBAPIError):
            with tenant_transaction(connection, tenant):
                connection.execute(text("INSERT INTO documents(id,tenant_id,filename,ingestion_state) "
                                        "VALUES (:id,:t,'unsafe','ready')"), {"id": uuid4(), "t": tenant})


def test_tenant_context_is_transaction_local_on_pooled_connection(postgres_engines, tenant_pair):
    _, app = postgres_engines
    a, b = tenant_pair
    with app.connect() as connection:
        assert not connection.in_transaction()
        with tenant_transaction(connection, a):
            assert connection.in_transaction()
            assert connection.execute(text("SELECT current_setting('app.tenant_id')")).scalar_one() == str(a)
        assert not connection.in_transaction()
        with connection.begin():
            value = connection.execute(text("SELECT nullif(current_setting('app.tenant_id',true),'')")).scalar_one_or_none()
            assert value is None
        with pytest.raises(ValueError, match="rollback tenant scope"):
            with tenant_transaction(connection, a):
                raise ValueError("rollback tenant scope")
        assert not connection.in_transaction()
        with connection.begin():
            value = connection.execute(text("SELECT nullif(current_setting('app.tenant_id',true),'')")).scalar_one_or_none()
            assert value is None
        with pytest.raises(RuntimeError, match="tenant_transaction_requires_idle_connection"):
            with connection.begin():
                with tenant_transaction(connection, a):
                    pass
        assert not connection.in_transaction()
        with tenant_transaction(connection, b):
            assert connection.execute(text("SELECT current_setting('app.tenant_id')")).scalar_one() == str(b)


def test_worker_wrong_tenant_fails_closed(postgres_engines, tenant_pair):
    _, app = postgres_engines
    tenant_a, tenant_b = tenant_pair
    document_id = uuid4()
    with app.connect() as connection, tenant_transaction(connection, tenant_a):
        TenantRepository(connection, tenant_a).insert_document(document_id, "staged")
    # A job claiming B cannot get A's row; repository mismatch is an explicit error.
    with app.connect() as connection, tenant_transaction(connection, tenant_b):
        assert TenantRepository(connection, tenant_b).get_document(document_id) is None
        with pytest.raises(PermissionError):
            TenantRepository(connection, tenant_a)


def test_runtime_role_is_not_owner_or_rls_bypass(postgres_engines):
    _, app = postgres_engines
    with app.connect() as connection:
        attributes = connection.execute(text("SELECT rolsuper,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user")).one()
        assert tuple(attributes) == (False, False, False)
        owner = connection.execute(text("SELECT pg_get_userbyid(c.relowner)=current_user FROM pg_class c WHERE c.relname='documents'")).scalar_one()
        assert owner is False


def test_all_tenant_tables_have_forced_using_and_check_rls(postgres_engines):
    _, app = postgres_engines
    expected = {
        "tenants", "users", "files", "documents", "document_versions", "audit_events",
        "fields", "data_corrections", "edit_sets", "patches", "reviews", "verification_manifests",
    }
    with app.connect() as connection:
        policies = connection.execute(text("SELECT tablename,qual,with_check FROM pg_policies WHERE schemaname='public'" )).all()
        found = {row[0]: (row[1], row[2]) for row in policies if row[0] in expected}
        assert set(found) == expected
        assert all("app.tenant_id" in using and "app.tenant_id" in check for using, check in found.values())
        forced = connection.execute(text("SELECT relname FROM pg_class WHERE relrowsecurity AND relforcerowsecurity")).scalars().all()
        assert expected <= set(forced)


def test_application_role_can_read_but_cannot_write_historical_reviews(postgres_engines):
    _, app = postgres_engines
    with app.connect() as connection:
        privileges = connection.execute(text("""SELECT
            has_table_privilege(current_user,'reviews','SELECT'),
            has_table_privilege(current_user,'reviews','INSERT'),
            has_table_privilege(current_user,'reviews','UPDATE'),
            has_table_privilege(current_user,'reviews','DELETE')""")).one()
    assert tuple(privileges) == (True, False, False, False)


def test_missing_authenticated_tenant_context_has_no_access(postgres_engines, tenant_pair):
    _, app = postgres_engines
    with app.connect() as connection:
        with connection.begin():
            assert connection.execute(text("SELECT count(*) FROM documents")).scalar_one() == 0
        with pytest.raises(DBAPIError):
            with connection.begin():
                connection.execute(text("INSERT INTO documents(id,tenant_id,filename) VALUES (:id,:t,'no-context')"),
                                   {"id": uuid4(), "t": tenant_pair[0]})
