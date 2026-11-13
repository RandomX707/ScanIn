"""Audit-only isolated storage reader for download-ineligible test versions.

This helper refuses shared database names and reads only storage below this repository's
ignored .runtime directory. It never changes database or file state.
"""
import os, sys
from pathlib import Path
from uuid import UUID
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
def database_name(name: str | None = None) -> str:
    value = name or os.environ.get("C360_ISO_DB", "channel360_iso_dev")
    if not value.startswith("channel360_iso_"):
        raise RuntimeError("storage reader requires a database named channel360_iso_*")
    return value


def database_url(name: str | None = None) -> str:
    return f"postgresql+psycopg://channel360_test_app@127.0.0.1:5432/{database_name(name)}"


def storage_root() -> Path:
    default = ROOT / ".runtime/iso-stack/storage"
    candidate = Path(os.environ.get("IDW_STORAGE_ROOT") or default).resolve()
    runtime = (ROOT / ".runtime").resolve()
    try:
        candidate.relative_to(runtime)
    except ValueError as exc:
        raise RuntimeError("storage reader requires a root below this repository's .runtime directory") from exc
    return candidate


def read_version(tenant_id: str, document_id: str, version_id: str) -> bytes:
    db = database_name()
    if "PGPASSFILE" not in os.environ:
        passfile = ROOT / ".runtime/iso-stack" / ("pgpass" if db == "channel360_iso_dev" else f"pgpass.{db}")
        os.environ["PGPASSFILE"] = str(passfile)
    from sqlalchemy import create_engine, text
    from packages.api.auth import AuthenticatedIdentity
    from packages.api.db_context import api_tenant_transaction
    eng = create_engine(database_url(db))
    ident = AuthenticatedIdentity(subject="audit", tenant_id=UUID(tenant_id), role="OWNER")
    with eng.connect() as c, api_tenant_transaction(c, ident):
        key = c.execute(text("""SELECT f.storage_key FROM document_versions v JOIN files f ON f.tenant_id=v.tenant_id AND f.id=v.file_id
            WHERE v.tenant_id=:t AND v.document_id=:d AND v.id=:v"""), {"t": tenant_id, "d": document_id, "v": version_id}).scalar_one()
    root = storage_root()
    path = (root / key).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("stored file path escaped isolated storage") from exc
    return path.read_bytes()
