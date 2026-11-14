"""Persist document lineage independently from paginated audit events."""
from alembic import op

revision = "0008_version_lineage_class"
down_revision = "0007_edit_set_supersession"
branch_labels = None
depends_on = None
_TABLES = ("document_versions", "audit_events")


def upgrade() -> None:
    # ACCESS EXCLUSIVE locks are taken on document_versions and audit_events for the whole transaction. If a long application transaction
    # holds either table, waiting forever would queue every new request behind this migration, so fail fast (the transaction rolls back
    # completely, FORCE ROW LEVEL SECURITY included) and let the operator retry in a quiet window.
    op.execute("SET LOCAL lock_timeout = '30s'")
    op.execute("ALTER TABLE document_versions ADD COLUMN lineage_class text NOT NULL DEFAULT 'unknown'")
    op.execute("""ALTER TABLE document_versions ADD CONSTRAINT document_versions_lineage_class_check
        CHECK (lineage_class IN ('original','standard','unsigned_working_copy','unknown'))""")
    # Every tenant table uses FORCE ROW LEVEL SECURITY, so the migration role would see ZERO rows and the backfill below would silently
    # change nothing (verified on PostgreSQL). The owner therefore lifts FORCE on the two tables it reads/updates for the duration of this
    # transaction (DDL is transactional), and a post-condition makes any remaining silent no-op fail the migration loudly.
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
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
    # Missing legacy provenance is not evidence that a version is an ordinary release: a derived version with neither a native edit set nor
    # a recorded release/save event is an UNKNOWN root, and unknown propagates to every descendant and branch. Everything else that was
    # not already marked a working copy is a standard release. Membership is computed with a recursive query (not by reading parents'
    # new classes inside the same UPDATE, which only sees the pre-update snapshot).
    op.execute("""WITH RECURSIVE unknown_roots(tenant_id,id) AS (
          SELECT v.tenant_id,v.id FROM document_versions v
          WHERE v.kind='derived' AND v.lineage_class='unknown' AND v.edit_set_id IS NULL
            AND NOT EXISTS (
              SELECT 1 FROM audit_events e WHERE e.tenant_id=v.tenant_id
                AND e.payload->>'version_id'=v.id::text
                AND e.action IN ('document.text_version_saved','document.derived_version_released')
            )
        ), unknown_descendants(tenant_id,id) AS (
          SELECT tenant_id,id FROM unknown_roots
          UNION
          SELECT child.tenant_id,child.id FROM document_versions child
            JOIN unknown_descendants parent ON child.tenant_id=parent.tenant_id AND child.parent_version_id=parent.id
        )
        UPDATE document_versions v SET lineage_class='standard'
        WHERE v.kind='derived' AND v.lineage_class='unknown'
          AND NOT EXISTS (SELECT 1 FROM unknown_descendants u WHERE u.tenant_id=v.tenant_id AND u.id=v.id)""")
    stale = op.get_bind().exec_driver_sql("SELECT count(*) FROM document_versions WHERE kind='original' AND lineage_class<>'original'").scalar()
    if stale:
        raise RuntimeError(f"lineage backfill incomplete: {stale} original versions were not classified")
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("ALTER TABLE document_versions DROP CONSTRAINT document_versions_lineage_class_check")
    op.execute("ALTER TABLE document_versions DROP COLUMN lineage_class")
