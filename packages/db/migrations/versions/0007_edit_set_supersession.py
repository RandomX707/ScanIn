"""Supersede stale edit sets when they are rebased onto the current document version.

An edit set (draft or ready_for_release) whose base version is stale can be rebased: a NEW draft
is created on the current version (``supersedes`` = predecessor) and the predecessor moves to the
terminal ``superseded`` status (``superseded_by`` = successor). The predecessor's frozen plan,
hash and patches are never rewritten. A unique index guarantees at most one successor per
predecessor, so repeated or concurrent rebases cannot create competing drafts.
"""
from alembic import op

revision = "0007_edit_set_supersession"
down_revision = "0006_owner_release_workflow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE edit_sets ADD COLUMN supersedes uuid")
    op.execute("ALTER TABLE edit_sets ADD COLUMN superseded_by uuid")
    op.execute("""ALTER TABLE edit_sets ADD CONSTRAINT edit_sets_supersedes_fk
      FOREIGN KEY (tenant_id,supersedes) REFERENCES edit_sets(tenant_id,id)""")
    op.execute("""ALTER TABLE edit_sets ADD CONSTRAINT edit_sets_superseded_by_fk
      FOREIGN KEY (tenant_id,superseded_by) REFERENCES edit_sets(tenant_id,id) DEFERRABLE INITIALLY DEFERRED""")
    op.execute("""CREATE UNIQUE INDEX edit_sets_one_successor
      ON edit_sets (tenant_id,supersedes) WHERE supersedes IS NOT NULL""")
    op.execute("ALTER TABLE edit_sets DROP CONSTRAINT edit_sets_status_check")
    op.execute("""ALTER TABLE edit_sets ADD CONSTRAINT edit_sets_status_check CHECK(status IN
      ('draft','ready_for_release','released','failed','superseded','submitted','approved','rejected'))""")
    op.execute("""ALTER TABLE edit_sets ADD CONSTRAINT edit_sets_superseded_link CHECK(
      (status='superseded') = (superseded_by IS NOT NULL) AND superseded_by IS DISTINCT FROM id
      AND supersedes IS DISTINCT FROM id)""")

    op.execute("DROP FUNCTION guard_edit_set_transition() CASCADE")
    op.execute("""CREATE FUNCTION guard_edit_set_transition() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP='INSERT' THEN
          IF NEW.status <> 'draft' OR NEW.frozen_hash IS NOT NULL OR NEW.patch_plan IS NOT NULL
             OR NEW.superseded_by IS NOT NULL THEN
            RAISE EXCEPTION 'edit sets must begin as unfrozen drafts' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END IF;
        IF ROW(OLD.id,OLD.tenant_id,OLD.document_id,OLD.base_version_id,OLD.created_by,OLD.reason,OLD.authorized_category,OLD.created_at,OLD.supersedes)
           IS DISTINCT FROM
           ROW(NEW.id,NEW.tenant_id,NEW.document_id,NEW.base_version_id,NEW.created_by,NEW.reason,NEW.authorized_category,NEW.created_at,NEW.supersedes) THEN
          RAISE EXCEPTION 'edit set authorization is immutable' USING ERRCODE='55000';
        END IF;
        IF OLD.status='superseded' THEN
          RAISE EXCEPTION 'superseded edit sets are immutable' USING ERRCODE='55000';
        END IF;
        IF OLD.status='draft' AND NEW.status='draft' THEN
          IF NEW.frozen_hash IS NOT NULL OR NEW.patch_plan IS NOT NULL OR NEW.superseded_by IS NOT NULL THEN
            RAISE EXCEPTION 'draft cannot contain a frozen plan' USING ERRCODE='55000';
          END IF;
          RETURN NEW;
        END IF;
        IF OLD.status='draft' AND NEW.status='ready_for_release' AND NEW.frozen_hash IS NOT NULL
           AND NEW.patch_plan IS NOT NULL AND NEW.adapter IS NOT NULL AND NEW.adapter_version IS NOT NULL THEN
          NULL;
        ELSIF OLD.status='ready_for_release' AND NEW.status IN ('released','failed') THEN
          NULL;
        ELSIF OLD.status IN ('draft','ready_for_release') AND NEW.status='superseded'
              AND NEW.superseded_by IS NOT NULL AND OLD.superseded_by IS NULL THEN
          NULL;
        ELSE
          RAISE EXCEPTION 'invalid edit set transition' USING ERRCODE='23514';
        END IF;
        IF NEW.status<>'superseded' AND NEW.superseded_by IS DISTINCT FROM OLD.superseded_by THEN
          RAISE EXCEPTION 'successor can be set only when superseding' USING ERRCODE='55000';
        END IF;
        IF OLD.status <> 'draft' AND ROW(OLD.frozen_hash,OLD.patch_plan,OLD.adapter,OLD.adapter_version)
           IS DISTINCT FROM ROW(NEW.frozen_hash,NEW.patch_plan,NEW.adapter,NEW.adapter_version) THEN
          RAISE EXCEPTION 'frozen plan is immutable' USING ERRCODE='55000';
        END IF;
        IF OLD.status='draft' AND NEW.status='superseded' AND (NEW.frozen_hash IS NOT NULL OR NEW.patch_plan IS NOT NULL) THEN
          RAISE EXCEPTION 'draft cannot contain a frozen plan' USING ERRCODE='55000';
        END IF;
        IF NEW.status='released' AND (NEW.released_version_id IS NULL OR OLD.released_version_id IS NOT NULL) THEN
          RAISE EXCEPTION 'release must attach exactly one derived version' USING ERRCODE='23514';
        END IF;
        IF NEW.status<>'released' AND NEW.released_version_id IS DISTINCT FROM OLD.released_version_id THEN
          RAISE EXCEPTION 'released version can be set only during release' USING ERRCODE='55000';
        END IF;
        RETURN NEW;
      END; $$""")
    op.execute("CREATE TRIGGER edit_set_transition_guard BEFORE INSERT OR UPDATE ON edit_sets FOR EACH ROW EXECUTE FUNCTION guard_edit_set_transition()")


def downgrade() -> None:
    raise RuntimeError("Edit-set supersession is not automatically reversible; superseded history must be preserved.")
