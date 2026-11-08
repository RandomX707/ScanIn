"""Persist document classification, frozen plans and derived-version lineage."""
from alembic import op

revision = "0004_native_release"
down_revision = "0003_corrections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE documents ADD COLUMN document_category text NOT NULL DEFAULT 'unknown'
      CHECK(document_category IN ('insurer-authored','customer-form','third-party-issued','signed-legal','unknown'))""")
    op.execute("ALTER TABLE edit_sets ADD COLUMN patch_plan jsonb")
    op.execute("ALTER TABLE edit_sets ADD COLUMN adapter text")
    op.execute("ALTER TABLE edit_sets ADD COLUMN adapter_version text")
    op.execute("ALTER TABLE edit_sets ADD COLUMN released_version_id uuid")
    op.execute("ALTER TABLE document_versions ADD COLUMN edit_set_id uuid")
    op.execute("""ALTER TABLE document_versions ADD CONSTRAINT document_versions_edit_set_fk
      FOREIGN KEY (tenant_id,edit_set_id) REFERENCES edit_sets(tenant_id,id)""")
    op.execute("""ALTER TABLE document_versions ADD CONSTRAINT document_versions_edit_set_unique
      UNIQUE (tenant_id,edit_set_id)""")
    op.execute("""ALTER TABLE edit_sets ADD CONSTRAINT edit_sets_released_version_fk
      FOREIGN KEY (tenant_id,released_version_id) REFERENCES document_versions(tenant_id,id)""")

    # Older lifecycle code already records that scanning occurs. This transition
    # makes its clean result explicit before isolated parsing begins.
    op.execute("""CREATE OR REPLACE FUNCTION guard_ingestion_state() RETURNS trigger LANGUAGE plpgsql AS $$
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
      END; $$""")

    # Authorization metadata and frozen plans are immutable after submission.
    op.execute("""CREATE OR REPLACE FUNCTION guard_edit_set_transition() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP='INSERT' THEN
          IF NEW.status <> 'draft' OR NEW.frozen_hash IS NOT NULL OR NEW.patch_plan IS NOT NULL THEN
            RAISE EXCEPTION 'edit sets must begin as unfrozen drafts' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END IF;
        IF ROW(OLD.id,OLD.tenant_id,OLD.document_id,OLD.base_version_id,OLD.created_by,OLD.reason,OLD.authorized_category,OLD.created_at)
           IS DISTINCT FROM
           ROW(NEW.id,NEW.tenant_id,NEW.document_id,NEW.base_version_id,NEW.created_by,NEW.reason,NEW.authorized_category,NEW.created_at) THEN
          RAISE EXCEPTION 'edit set authorization is immutable' USING ERRCODE='55000';
        END IF;
        IF OLD.status='draft' AND NEW.status='draft' THEN
          IF NEW.frozen_hash IS NOT NULL OR NEW.patch_plan IS NOT NULL THEN
            RAISE EXCEPTION 'draft cannot contain a frozen plan' USING ERRCODE='55000';
          END IF;
          RETURN NEW;
        END IF;
        IF (OLD.status='draft' AND NEW.status='submitted' AND NEW.frozen_hash IS NOT NULL
            AND NEW.patch_plan IS NOT NULL AND NEW.adapter IS NOT NULL AND NEW.adapter_version IS NOT NULL) OR
           (OLD.status='submitted' AND NEW.status IN ('approved','rejected','failed')) OR
           (OLD.status='approved' AND NEW.status IN ('released','failed')) THEN
          IF OLD.status <> 'draft' AND ROW(OLD.frozen_hash,OLD.patch_plan,OLD.adapter,OLD.adapter_version)
             IS DISTINCT FROM ROW(NEW.frozen_hash,NEW.patch_plan,NEW.adapter,NEW.adapter_version) THEN
            RAISE EXCEPTION 'frozen plan is immutable' USING ERRCODE='55000';
          END IF;
          IF NEW.status='released' AND (NEW.released_version_id IS NULL OR OLD.released_version_id IS NOT NULL) THEN
            RAISE EXCEPTION 'release must attach exactly one derived version' USING ERRCODE='23514';
          END IF;
          IF NEW.status<>'released' AND NEW.released_version_id IS DISTINCT FROM OLD.released_version_id THEN
            RAISE EXCEPTION 'released version can be set only during release' USING ERRCODE='55000';
          END IF;
          RETURN NEW;
        END IF;
        RAISE EXCEPTION 'invalid edit set transition' USING ERRCODE='23514';
      END; $$""")


def downgrade() -> None:
    op.execute("ALTER TABLE edit_sets DROP CONSTRAINT edit_sets_released_version_fk")
    op.execute("ALTER TABLE document_versions DROP CONSTRAINT document_versions_edit_set_unique")
    op.execute("ALTER TABLE document_versions DROP CONSTRAINT document_versions_edit_set_fk")
    op.execute("ALTER TABLE edit_sets DROP COLUMN released_version_id, DROP COLUMN adapter_version, DROP COLUMN adapter, DROP COLUMN patch_plan")
    op.execute("ALTER TABLE document_versions DROP COLUMN edit_set_id")
    op.execute("ALTER TABLE documents DROP COLUMN document_category")
