"""Persist document lineage independently from paginated audit events."""
from alembic import op

revision = "0008_version_lineage_class"
down_revision = "0007_edit_set_supersession"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE document_versions ADD COLUMN lineage_class text NOT NULL DEFAULT 'unknown'")
    op.execute("""ALTER TABLE document_versions ADD CONSTRAINT document_versions_lineage_class_check
        CHECK (lineage_class IN ('original','standard','unsigned_working_copy','unknown'))""")
    op.execute("UPDATE document_versions SET lineage_class='original' WHERE kind='original'")
    # Audit events are the only legacy source for some unsigned working copies. Expand every
    # known root through parent links before removing API dependence on a recent-event page.
    op.execute("""WITH RECURSIVE roots(tenant_id,id) AS (
          SELECT e.tenant_id,(e.payload->>'version_id')::uuid
          FROM audit_events e JOIN document_versions v ON v.tenant_id=e.tenant_id
            AND v.id::text=e.payload->>'version_id'
          WHERE e.action='document.text_version_saved'
            AND e.payload->>'unsigned_working_copy'='true'
        ), descendants(tenant_id,id) AS (
          SELECT tenant_id,id FROM roots
          UNION
          SELECT child.tenant_id,child.id FROM document_versions child
            JOIN descendants parent ON child.tenant_id=parent.tenant_id AND child.parent_version_id=parent.id
        )
        UPDATE document_versions v SET lineage_class='unsigned_working_copy'
        FROM descendants d WHERE v.tenant_id=d.tenant_id AND v.id=d.id""")
    # Versions with authoritative native release manifests are standard unless they descend
    # from a working copy (already marked above). Unknown legacy rows stay visibly unknown.
    op.execute("""UPDATE document_versions v SET lineage_class='standard'
        WHERE v.kind='derived' AND v.lineage_class='unknown' AND (
          v.edit_set_id IS NOT NULL OR EXISTS (
            SELECT 1 FROM audit_events e WHERE e.tenant_id=v.tenant_id
              AND e.payload->>'version_id'=v.id::text
              AND e.action IN ('document.text_version_saved','document.derived_version_released')
          ))""")


def downgrade() -> None:
    op.execute("ALTER TABLE document_versions DROP CONSTRAINT document_versions_lineage_class_check")
    op.execute("ALTER TABLE document_versions DROP COLUMN lineage_class")
