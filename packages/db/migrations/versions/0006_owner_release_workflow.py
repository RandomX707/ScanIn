"""Replace human review with an owner-authorized, verified release transition.

Legacy submitted/approved/rejected statuses and review rows remain readable as
historical records. New edit sets use draft -> ready_for_release -> released.
"""
from alembic import op

revision = "0006_owner_release_workflow"
down_revision = "0005_docx_verification_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Retain legacy identity values for old records; OWNER is the only new
    # application identity emitted by the API and local-demo session.
    op.execute("ALTER TABLE users DROP CONSTRAINT users_role_check")
    op.execute("ALTER TABLE users ADD CONSTRAINT users_role_check CHECK(role IN ('OWNER','processor','reviewer','administrator'))")

    op.execute("ALTER TABLE edit_sets DROP CONSTRAINT edit_sets_status_check")
    op.execute("ALTER TABLE edit_sets ADD CONSTRAINT edit_sets_status_check CHECK(status IN ('draft','ready_for_release','released','failed','submitted','approved','rejected'))")

    op.execute("DROP FUNCTION guard_edit_set_transition() CASCADE")
    op.execute("""CREATE FUNCTION guard_edit_set_transition() RETURNS trigger LANGUAGE plpgsql AS $$
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
        IF OLD.status='draft' AND NEW.status='ready_for_release' AND NEW.frozen_hash IS NOT NULL
           AND NEW.patch_plan IS NOT NULL AND NEW.adapter IS NOT NULL AND NEW.adapter_version IS NOT NULL THEN
          NULL;
        ELSIF OLD.status='ready_for_release' AND NEW.status IN ('released','failed') THEN
          NULL;
        ELSE
          RAISE EXCEPTION 'invalid edit set transition' USING ERRCODE='23514';
        END IF;
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
      END; $$""")
    op.execute("CREATE TRIGGER edit_set_transition_guard BEFORE INSERT OR UPDATE ON edit_sets FOR EACH ROW EXECUTE FUNCTION guard_edit_set_transition()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS edit_set_transition_guard ON edit_sets")
    op.execute("DROP FUNCTION IF EXISTS guard_edit_set_transition()")
    raise RuntimeError("Owner-release workflow migration is not automatically reversible; preserve historical edit-set states.")
