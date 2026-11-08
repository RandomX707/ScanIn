"""Attach each document to its current immutable source version."""
from alembic import op

revision = "0002_current_version"
down_revision = "0001_tenant_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE documents ADD COLUMN current_version_id uuid")
    op.execute("""ALTER TABLE documents ADD CONSTRAINT documents_current_version_fk
                 FOREIGN KEY (tenant_id,current_version_id)
                 REFERENCES document_versions(tenant_id,id) DEFERRABLE INITIALLY DEFERRED""")
    op.execute("ALTER TABLE document_versions DROP CONSTRAINT document_versions_tenant_id_document_id_fkey")
    op.execute("""ALTER TABLE document_versions ADD CONSTRAINT document_versions_document_fk
                 FOREIGN KEY (tenant_id,document_id) REFERENCES documents(tenant_id,id) ON DELETE CASCADE""")


def downgrade() -> None:
    op.execute("ALTER TABLE document_versions DROP CONSTRAINT document_versions_document_fk")
    op.execute("""ALTER TABLE document_versions ADD CONSTRAINT document_versions_tenant_id_document_id_fkey
                 FOREIGN KEY (tenant_id,document_id) REFERENCES documents(tenant_id,id)""")
    op.execute("ALTER TABLE documents DROP CONSTRAINT documents_current_version_fk")
    op.execute("ALTER TABLE documents DROP COLUMN current_version_id")
