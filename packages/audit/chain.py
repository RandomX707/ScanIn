from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from packages.db.tenant import require_tenant_context


GENESIS_HASH = "0" * 64


def canonical_event_bytes(*, tenant_id: UUID, sequence: int, event_id: UUID, timestamp: datetime,
                          actor_id: str, action: str, subject: str, payload: dict[str, object],
                          previous_hash: str) -> bytes:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("audit timestamps must include a timezone")
    normalized = {
        "action": action,
        "actor_id": actor_id,
        "event_id": str(event_id),
        "payload": payload,
        "previous_hash": previous_hash,
        "sequence": sequence,
        "subject": subject,
        "tenant_id": str(tenant_id),
        "timestamp": timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
    }
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def hash_event(**kwargs: object) -> str:
    return hashlib.sha256(canonical_event_bytes(**kwargs)).hexdigest()


@dataclass(frozen=True)
class AuditEvent:
    event_id: UUID
    tenant_id: UUID
    sequence: int
    timestamp: datetime
    actor_id: str
    action: str
    subject: str
    payload: dict[str, object]
    previous_hash: str
    event_hash: str


class DailyDigestSigner(Protocol):
    """Signing hook; production implementation should use a managed asymmetric key."""
    def sign(self, message: bytes) -> bytes: ...


class HMACDevelopmentSigner:
    """Local/test signer only. Not suitable for production non-repudiation."""
    def __init__(self, secret: bytes):
        if len(secret) < 32:
            raise ValueError("digest signing key must be at least 32 bytes")
        self._secret = secret

    def sign(self, message: bytes) -> bytes:
        return hmac.new(self._secret, message, hashlib.sha256).digest()


def append_event(connection: Connection, *, actor_id: str, action: str, subject: str,
                 payload: dict[str, object], timestamp: datetime | None = None) -> AuditEvent:
    return append_events(connection, actor_id=actor_id,
                         events=((action, subject, payload),), timestamp=timestamp)[0]


def append_events(connection: Connection, *, actor_id: str,
                  events: tuple[tuple[str, str, dict[str, object]], ...],
                  timestamp: datetime | None = None) -> list[AuditEvent]:
    """Append a transaction's events while acquiring the tenant-chain head once."""
    if not events:
        return []
    tenant_id = require_tenant_context(connection)
    # Locking the tenant head row serializes all writers for a tenant in this transaction.
    head = connection.execute(
        # Audit writers update only non-key head columns. NO KEY UPDATE remains
        # compatible with tenant FK KEY SHARE locks held earlier in workflows,
        # avoiding lock-upgrade deadlocks during concurrent releases.
        text("SELECT audit_sequence, audit_head_hash FROM tenants WHERE id=:tenant FOR NO KEY UPDATE"),
        {"tenant": tenant_id},
    ).mappings().one()
    sequence = int(head["audit_sequence"]) + 1
    previous = head["audit_head_hash"] or GENESIS_HASH
    appended: list[AuditEvent] = []
    insert = text("""INSERT INTO audit_events
        (id,tenant_id,sequence,ts,actor_id,action,subject,payload,prev_hash,event_hash)
        VALUES (:id,:tenant,:sequence,:ts,:actor,:action,:subject,CAST(:payload AS jsonb),:previous,:digest)""")
    for action, subject, payload in events:
        now = timestamp or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("audit timestamps must include a timezone")
        now = now.astimezone(timezone.utc)
        event_id = uuid4()
        digest = hash_event(tenant_id=tenant_id, sequence=sequence, event_id=event_id, timestamp=now,
                            actor_id=actor_id, action=action, subject=subject, payload=payload,
                            previous_hash=previous)
        connection.execute(insert, {"id": event_id, "tenant": tenant_id, "sequence": sequence,
            "ts": now, "actor": actor_id, "action": action, "subject": subject,
            "payload": json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False),
            "previous": previous, "digest": digest})
        appended.append(AuditEvent(event_id, tenant_id, sequence, now, actor_id, action, subject,
                                   payload, previous, digest))
        sequence += 1
        previous = digest
    connection.execute(text("UPDATE tenants SET audit_sequence=:sequence, audit_head_hash=:digest WHERE id=:tenant"),
                       {"sequence": sequence - 1, "digest": previous, "tenant": tenant_id})
    return appended


def verify_chain(connection: Connection, tenant_id: UUID) -> tuple[bool, str | None]:
    if require_tenant_context(connection) != tenant_id:
        raise PermissionError("audit verification tenant does not match transaction context")
    rows = connection.execute(text("SELECT id,sequence,ts,actor_id,action,subject,payload,prev_hash,event_hash "
                                   "FROM audit_events WHERE tenant_id=:tenant ORDER BY sequence"),
                              {"tenant": tenant_id}).mappings()
    previous = GENESIS_HASH
    expected_sequence = 1
    for row in rows:
        if row["sequence"] != expected_sequence or row["prev_hash"] != previous:
            return False, f"chain_link_invalid_at_sequence_{expected_sequence}"
        actual = hash_event(tenant_id=tenant_id, sequence=row["sequence"], event_id=row["id"],
                            timestamp=row["ts"], actor_id=row["actor_id"], action=row["action"],
                            subject=row["subject"], payload=row["payload"], previous_hash=previous)
        if not hmac.compare_digest(actual, row["event_hash"]):
            return False, f"event_hash_invalid_at_sequence_{expected_sequence}"
        previous = actual
        expected_sequence += 1
    head = connection.execute(text("SELECT audit_sequence,audit_head_hash FROM tenants WHERE id=:tenant"),
                              {"tenant": tenant_id}).mappings().one()
    if int(head["audit_sequence"]) != expected_sequence - 1 or (head["audit_head_hash"] or GENESIS_HASH) != previous:
        return False, "tenant_chain_head_mismatch"
    return True, None


def signed_daily_digest(connection: Connection, tenant_id: UUID, day: str, signer: DailyDigestSigner) -> dict[str, str | int]:
    """Return signed digest material for a WORM writer; persistence is an integration hook."""
    if require_tenant_context(connection) != tenant_id:
        raise PermissionError("digest tenant does not match transaction context")
    head = connection.execute(text("SELECT audit_sequence,audit_head_hash FROM tenants WHERE id=:tenant"),
                              {"tenant": tenant_id}).mappings().one()
    message = f"{tenant_id}\n{day}\n{head['audit_sequence']}\n{head['audit_head_hash'] or GENESIS_HASH}".encode()
    signature = signer.sign(message)
    return {"tenant_id": str(tenant_id), "day": day, "sequence": int(head["audit_sequence"]),
            "head_hash": head["audit_head_hash"] or GENESIS_HASH, "signature": signature.hex()}
