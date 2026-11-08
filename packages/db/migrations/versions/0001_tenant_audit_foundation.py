"""Tenant-owned foundation tables, forced RLS and append-only audit chain."""
from alembic import op

revision = "0001_tenant_audit"
down_revision = None
branch_labels = None
depends_on = None

TENANT_TABLES = ("users", "files", "documents", "document_versions", "audit_events")

def upgrade() -> None:
    op.execute("""
      CREATE TABLE tenants (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), name text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), audit_sequence bigint NOT NULL DEFAULT 0 CHECK(audit_sequence>=0),
        audit_head_hash char(64) NOT NULL DEFAULT repeat('0',64)
      )
    """)
    op.execute("""
      CREATE TABLE users (
        id uuid NOT NULL DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
        subject text NOT NULL, email text NOT NULL, role text NOT NULL CHECK(role IN ('processor','reviewer','administrator')),
        created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,subject), UNIQUE(tenant_id,email)
      )
    """)
    op.execute("""
      CREATE TABLE files (
        id uuid NOT NULL DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
        sha256 char(64) NOT NULL, size_bytes bigint NOT NULL CHECK(size_bytes>=0), mime text NOT NULL,
        storage_key text NOT NULL, file_role text NOT NULL CHECK(file_role IN ('original','preview','derived','sidecar')),
        created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,storage_key)
      )
    """)
    op.execute("""
      CREATE TABLE documents (
        id uuid NOT NULL DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
        filename text NOT NULL, created_by uuid, created_at timestamptz NOT NULL DEFAULT now(),
        ingestion_state text NOT NULL DEFAULT 'staged_private' CHECK(ingestion_state IN
          ('staged_private','scan_pending','scanning','scan_clean','quarantined','rejected','inspecting','ready','failed')),
        PRIMARY KEY(tenant_id,id), FOREIGN KEY(tenant_id,created_by) REFERENCES users(tenant_id,id)
      )
    """)
    op.execute("""
      CREATE TABLE document_versions (
        id uuid NOT NULL DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, document_id uuid NOT NULL,
        parent_version_id uuid, file_id uuid, kind text NOT NULL CHECK(kind IN ('original','preview','derived')),
        sha256 char(64) NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(tenant_id,id),
        FOREIGN KEY(tenant_id,document_id) REFERENCES documents(tenant_id,id),
        FOREIGN KEY(tenant_id,parent_version_id) REFERENCES document_versions(tenant_id,id),
        FOREIGN KEY(tenant_id,file_id) REFERENCES files(tenant_id,id)
      )
    """)
    op.execute("""
      CREATE TABLE audit_events (
        id uuid NOT NULL, tenant_id uuid NOT NULL REFERENCES tenants(id), sequence bigint NOT NULL CHECK(sequence>0),
        ts timestamptz NOT NULL, actor_id text NOT NULL, action text NOT NULL, subject text NOT NULL,
        payload jsonb NOT NULL, prev_hash char(64) NOT NULL, event_hash char(64) NOT NULL,
        PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,sequence)
      )
    """)
    op.execute("""
      CREATE FUNCTION guard_ingestion_state() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP = 'INSERT' THEN
          IF NEW.ingestion_state <> 'staged_private' THEN
            RAISE EXCEPTION 'documents must begin staged_private' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END IF;
        IF NOT (
          (OLD.ingestion_state='staged_private' AND NEW.ingestion_state IN ('scan_pending','rejected')) OR
          (OLD.ingestion_state='scan_pending' AND NEW.ingestion_state IN ('scanning','failed')) OR
          (OLD.ingestion_state='scanning' AND NEW.ingestion_state IN ('scan_clean','quarantined','failed')) OR
          (OLD.ingestion_state='scan_clean' AND NEW.ingestion_state IN ('inspecting','failed')) OR
          (OLD.ingestion_state='inspecting' AND NEW.ingestion_state IN ('ready','failed'))
        ) THEN
          RAISE EXCEPTION 'invalid ingestion state transition' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
      END; $$
    """)
    op.execute("CREATE TRIGGER documents_state_guard BEFORE INSERT OR UPDATE OF ingestion_state ON documents FOR EACH ROW EXECUTE FUNCTION guard_ingestion_state()")
    # Missing/empty tenant context yields no visible rows. WITH CHECK also blocks forged tenant inserts.
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)")
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenants FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON tenants USING (id = nullif(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (id = nullif(current_setting('app.tenant_id', true), '')::uuid)")
    op.execute("""
      CREATE FUNCTION reject_audit_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'audit events are append-only' USING ERRCODE='55000'; END; $$
    """)
    op.execute("CREATE TRIGGER audit_no_update_delete BEFORE UPDATE OR DELETE ON audit_events FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation()")

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_events, document_versions, documents, files, users, tenants CASCADE")
    op.execute("DROP FUNCTION IF EXISTS guard_ingestion_state()")
    op.execute("DROP FUNCTION IF EXISTS reject_audit_mutation()")
