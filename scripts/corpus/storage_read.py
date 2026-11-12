"""Audit-only: read a stored version's bytes straight from the isolated test stack's storage (bypassing the API's
download policy, which correctly withholds INCOMPLETE derivatives). Tenant-scoped query through the app role."""
import os, sys
from pathlib import Path
from uuid import UUID
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
STORAGE = ROOT / ".runtime/test-stack/storage"


def read_version(tenant_id: str, document_id: str, version_id: str) -> bytes:
    os.environ.setdefault("PGPASSFILE", str(Path.home() / ".channel360-pgpass"))
    from sqlalchemy import create_engine, text
    from packages.api.auth import AuthenticatedIdentity
    from packages.api.db_context import api_tenant_transaction
    eng = create_engine("postgresql+psycopg://channel360_test_app@127.0.0.1:5432/channel360_test")
    ident = AuthenticatedIdentity(subject="audit", tenant_id=UUID(tenant_id), role="OWNER")
    with eng.connect() as c, api_tenant_transaction(c, ident):
        key = c.execute(text("""SELECT f.storage_key FROM document_versions v JOIN files f ON f.tenant_id=v.tenant_id AND f.id=v.file_id
            WHERE v.tenant_id=:t AND v.document_id=:d AND v.id=:v"""), {"t": tenant_id, "d": document_id, "v": version_id}).scalar_one()
    return (STORAGE / key).read_bytes()
