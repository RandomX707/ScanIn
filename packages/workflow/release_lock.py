from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import text


class ReleaseInProgress(RuntimeError):
    pass


def _release_lock_key(edit_set_id: str) -> int:
    return int.from_bytes(hashlib.sha256(edit_set_id.encode("ascii")).digest()[:8], "big", signed=True)


@contextmanager
def claim_release(engine, edit_set_id: str) -> Iterator[None]:
    """Claim a per-edit-set PostgreSQL session lock; disconnect releases crashed claims."""
    key = _release_lock_key(edit_set_id)
    with engine.connect() as connection:
        acquired = bool(connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar_one())
        connection.commit()
        if not acquired:
            raise ReleaseInProgress("release_in_progress")
        try:
            yield
        finally:
            try:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                connection.commit()
            except Exception:
                # Closing an invalidated connection forces PostgreSQL to release its session lock.
                connection.invalidate()
