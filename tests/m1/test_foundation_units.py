from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from packages.audit.chain import GENESIS_HASH, HMACDevelopmentSigner, canonical_event_bytes, hash_event
from packages.db.worker_context import ValidatedJobContext
from packages.registry.lifecycle import IngestionState as State, content_available_to_normal_viewers, transition


def test_staged_untrusted_documents_remain_private_until_ready():
    assert content_available_to_normal_viewers(State.STAGED_PRIVATE) is False
    assert content_available_to_normal_viewers(State.SCAN_CLEAN) is False
    assert transition(State.STAGED_PRIVATE, State.SCAN_PENDING) is State.SCAN_PENDING
    assert transition(State.SCANNING, State.QUARANTINED) is State.QUARANTINED
    assert content_available_to_normal_viewers(State.READY) is True
    with pytest.raises(ValueError, match="invalid_ingestion_transition"):
        transition(State.STAGED_PRIVATE, State.READY)


def test_job_context_rejects_incorrect_document_tenant():
    with pytest.raises(PermissionError, match="job tenant does not match"):
        ValidatedJobContext(uuid4(), uuid4())


def test_audit_event_hash_is_deterministic_and_payload_order_independent():
    tenant, event = uuid4(), uuid4()
    timestamp = datetime(2026, 11, 6, tzinfo=timezone.utc)
    args = dict(tenant_id=tenant, sequence=1, event_id=event, timestamp=timestamp,
                actor_id="actor", action="created", subject="document", previous_hash=GENESIS_HASH)
    assert hash_event(**args, payload={"a": 1, "b": 2}) == hash_event(**args, payload={"b": 2, "a": 1})


def test_development_signer_requires_sufficient_key_material():
    with pytest.raises(ValueError):
        HMACDevelopmentSigner(b"too-short")


def test_canonical_audit_hash_rejects_naive_timestamps():
    with pytest.raises(ValueError, match="timezone"):
        canonical_event_bytes(tenant_id=uuid4(), sequence=1, event_id=uuid4(), timestamp=datetime(2026, 1, 1),
                              actor_id="a", action="b", subject="c", payload={}, previous_hash="0" * 64)
