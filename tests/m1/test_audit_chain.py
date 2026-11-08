from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from packages.audit.chain import HMACDevelopmentSigner, append_event, signed_daily_digest, verify_chain
from packages.db.tenant import tenant_transaction

pytestmark = pytest.mark.postgres


def test_concurrent_writers_are_serialized_and_chain_is_contiguous(postgres_engines, tenant_pair):
    _, app = postgres_engines
    tenant = tenant_pair[0]
    count = 12
    def write(index: int) -> int:
        with app.connect() as connection, tenant_transaction(connection, tenant):
            event = append_event(connection, actor_id="test", action="write", subject=f"item:{index}", payload={"i": index})
            return event.sequence
    with ThreadPoolExecutor(max_workers=6) as pool:
        sequences = list(pool.map(write, range(count)))
    assert sorted(sequences) == list(range(1, count + 1))
    with app.connect() as connection, tenant_transaction(connection, tenant):
        assert verify_chain(connection, tenant) == (True, None)


def test_event_update_and_delete_are_denied_to_application_role(postgres_engines, tenant_pair):
    _, app = postgres_engines
    tenant = tenant_pair[0]
    with app.connect() as connection, tenant_transaction(connection, tenant):
        event = append_event(connection, actor_id="test", action="created", subject="x", payload={})
        for statement in (
            "UPDATE audit_events SET action='tampered' WHERE tenant_id=:t AND id=:id",
            "DELETE FROM audit_events WHERE tenant_id=:t AND id=:id",
        ):
            with pytest.raises(DBAPIError):
                with connection.begin_nested():
                    connection.execute(text(statement), {"t": tenant, "id": event.event_id})
    with app.connect() as connection, tenant_transaction(connection, tenant):
        assert verify_chain(connection, tenant) == (True, None)


def test_owner_level_event_tampering_is_detected(postgres_engines, tenant_pair):
    owner, app = postgres_engines
    tenant = tenant_pair[0]
    with app.connect() as connection, tenant_transaction(connection, tenant):
        event = append_event(connection, actor_id="test", action="created", subject="x", payload={"v": 1})
    # Deliberately use migration credentials to model a privileged database operator.
    with owner.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id',:tenant,true)"), {"tenant": str(tenant)})
        connection.execute(text("ALTER TABLE audit_events DISABLE TRIGGER audit_no_update_delete"))
        connection.execute(text("UPDATE audit_events SET payload=CAST(:payload AS jsonb) WHERE tenant_id=:t AND id=:id"),
                           {"t": tenant, "id": event.event_id, "payload": '{"v":2}'})
        connection.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_no_update_delete"))
    with app.connect() as connection, tenant_transaction(connection, tenant):
        valid, reason = verify_chain(connection, tenant)
        assert valid is False and reason == "event_hash_invalid_at_sequence_1"


def test_owner_level_deletion_is_detected_by_tenant_chain_head(postgres_engines, tenant_pair):
    owner, app = postgres_engines
    tenant = tenant_pair[0]
    with app.connect() as connection, tenant_transaction(connection, tenant):
        event = append_event(connection, actor_id="test", action="created", subject="x", payload={})
    with owner.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id',:tenant,true)"), {"tenant": str(tenant)})
        connection.execute(text("ALTER TABLE audit_events DISABLE TRIGGER audit_no_update_delete"))
        connection.execute(text("DELETE FROM audit_events WHERE tenant_id=:t AND id=:id"),
                           {"t": tenant, "id": event.event_id})
        connection.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_no_update_delete"))
    with app.connect() as connection, tenant_transaction(connection, tenant):
        valid, reason = verify_chain(connection, tenant)
        assert valid is False and reason == "tenant_chain_head_mismatch"


def test_daily_digest_signing_hook_returns_signed_chain_head(postgres_engines, tenant_pair):
    _, app = postgres_engines
    tenant = tenant_pair[0]
    with app.connect() as connection, tenant_transaction(connection, tenant):
        append_event(connection, actor_id="test", action="created", subject="x", payload={})
        digest = signed_daily_digest(connection, tenant, "2026-11-06", HMACDevelopmentSigner(b"x" * 32))
    assert digest["tenant_id"] == str(tenant)
    assert digest["sequence"] == 1
    assert len(digest["signature"]) == 64
