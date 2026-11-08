"""Tenant-scoped fields, corrections, frozen edit sets, reviews and manifests."""
from alembic import op

revision = "0003_corrections"
down_revision = "0002_current_version"
branch_labels = None
depends_on = None

TENANT_TABLES = ("fields", "data_corrections", "edit_sets", "patches", "reviews", "verification_manifests")


def upgrade() -> None:
    op.execute("""CREATE TABLE fields (
      id uuid NOT NULL DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, document_id uuid NOT NULL,
      version_id uuid NOT NULL, field_key text, label text NOT NULL, source text NOT NULL
        CHECK(source IN ('manual','template','plugin')),
      observed_value jsonb NOT NULL, target_id text, target_fingerprint char(64),
      evidence jsonb NOT NULL DEFAULT '[]'::jsonb, signals jsonb NOT NULL DEFAULT '{}'::jsonb,
      review_status text NOT NULL DEFAULT 'unreviewed' CHECK(review_status IN ('unreviewed','accepted','corrected')),
      created_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(tenant_id,id),
      FOREIGN KEY(tenant_id,document_id) REFERENCES documents(tenant_id,id),
      FOREIGN KEY(tenant_id,version_id) REFERENCES document_versions(tenant_id,id)
    )""")
    op.execute("""CREATE TABLE data_corrections (
      id uuid NOT NULL DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, field_id uuid NOT NULL,
      source_version_id uuid NOT NULL, corrected_value jsonb NOT NULL, reason text NOT NULL CHECK(length(trim(reason))>0),
      actor_id text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,id),
      FOREIGN KEY(tenant_id,field_id) REFERENCES fields(tenant_id,id),
      FOREIGN KEY(tenant_id,source_version_id) REFERENCES document_versions(tenant_id,id)
    )""")
    op.execute("""CREATE TABLE edit_sets (
      id uuid NOT NULL DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, document_id uuid NOT NULL,
      base_version_id uuid NOT NULL, status text NOT NULL DEFAULT 'draft'
        CHECK(status IN ('draft','submitted','approved','rejected','released','failed')),
      frozen_hash char(64), authorized_category text NOT NULL DEFAULT 'unknown'
        CHECK(authorized_category IN ('insurer-authored','customer-form','third-party-issued','signed-legal','unknown')),
      created_by text NOT NULL, reason text NOT NULL CHECK(length(trim(reason))>0),
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(tenant_id,id),
      FOREIGN KEY(tenant_id,document_id) REFERENCES documents(tenant_id,id),
      FOREIGN KEY(tenant_id,base_version_id) REFERENCES document_versions(tenant_id,id)
    )""")
    op.execute("""CREATE TABLE patches (
      id uuid NOT NULL DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, edit_set_id uuid NOT NULL,
      target_id text NOT NULL, expected_old_value jsonb NOT NULL, expected_fingerprint char(64) NOT NULL,
      new_value jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,id),
      UNIQUE(tenant_id,edit_set_id,target_id),
      FOREIGN KEY(tenant_id,edit_set_id) REFERENCES edit_sets(tenant_id,id) ON DELETE CASCADE
    )""")
    op.execute("""CREATE TABLE reviews (
      id uuid NOT NULL DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, edit_set_id uuid NOT NULL,
      edit_set_hash char(64) NOT NULL, reviewer_id text NOT NULL,
      decision text NOT NULL CHECK(decision IN ('approved','rejected')), note text,
      created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,id),
      FOREIGN KEY(tenant_id,edit_set_id) REFERENCES edit_sets(tenant_id,id)
    )""")
    op.execute("""CREATE TABLE verification_manifests (
      id uuid NOT NULL DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, edit_set_id uuid NOT NULL,
      source_sha256 char(64) NOT NULL, output_sha256 char(64), outcome text NOT NULL
        CHECK(outcome IN ('VERIFIED','INCOMPLETE','BLOCKED')),
      adapter text NOT NULL, adapter_version text NOT NULL, manifest jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,id),
      FOREIGN KEY(tenant_id,edit_set_id) REFERENCES edit_sets(tenant_id,id)
    )""")

    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)")

    # Corrections, reviews, and verification records are append-only for every role.
    op.execute("CREATE TRIGGER corrections_append_only BEFORE UPDATE OR DELETE ON data_corrections FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation()")
    op.execute("CREATE TRIGGER reviews_append_only BEFORE UPDATE OR DELETE ON reviews FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation()")
    op.execute("CREATE TRIGGER manifests_append_only BEFORE UPDATE OR DELETE ON verification_manifests FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation()")
    # A patch row is mutable only while its parent edit set is a draft. The row
    # lock serializes patch edits against submission/freeze of the parent.
    op.execute("""CREATE FUNCTION guard_patch_draft() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE edit_status text; set_id uuid; tenant uuid;
      BEGIN
        IF TG_OP='DELETE' THEN set_id := OLD.edit_set_id; tenant := OLD.tenant_id;
        ELSE set_id := NEW.edit_set_id; tenant := NEW.tenant_id; END IF;
        IF TG_OP='UPDATE' AND (OLD.tenant_id IS DISTINCT FROM NEW.tenant_id OR OLD.edit_set_id IS DISTINCT FROM NEW.edit_set_id) THEN
          RAISE EXCEPTION 'patch identity is immutable' USING ERRCODE='55000';
        END IF;
        SELECT status INTO edit_status FROM edit_sets WHERE tenant_id=tenant AND id=set_id FOR UPDATE;
        IF edit_status IS DISTINCT FROM 'draft' THEN
          RAISE EXCEPTION 'patches are mutable only while draft' USING ERRCODE='55000';
        END IF;
        IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
      END; $$""")
    op.execute("CREATE TRIGGER patches_draft_guard BEFORE INSERT OR UPDATE OR DELETE ON patches FOR EACH ROW EXECUTE FUNCTION guard_patch_draft()")
    op.execute("""CREATE FUNCTION guard_edit_set_transition() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP='INSERT' THEN
          IF NEW.status <> 'draft' OR NEW.frozen_hash IS NOT NULL THEN
            RAISE EXCEPTION 'edit sets must begin as unfrozen drafts' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END IF;
        IF OLD.status=NEW.status AND OLD.status='draft' THEN RETURN NEW; END IF;
        IF (OLD.status='draft' AND NEW.status='submitted' AND NEW.frozen_hash IS NOT NULL) OR
           (OLD.status='submitted' AND NEW.status IN ('approved','rejected','failed')) OR
           (OLD.status='approved' AND NEW.status IN ('released','failed')) OR
           (OLD.status IN ('draft','submitted','approved') AND NEW.status='failed') THEN
          IF ROW(OLD.id,OLD.tenant_id,OLD.document_id,OLD.base_version_id,OLD.created_by,OLD.reason,OLD.authorized_category,OLD.created_at)
             IS DISTINCT FROM
             ROW(NEW.id,NEW.tenant_id,NEW.document_id,NEW.base_version_id,NEW.created_by,NEW.reason,NEW.authorized_category,NEW.created_at) THEN
            RAISE EXCEPTION 'edit set authorization is immutable' USING ERRCODE='55000';
          END IF;
          IF OLD.status <> 'draft' AND NEW.frozen_hash IS DISTINCT FROM OLD.frozen_hash THEN
            RAISE EXCEPTION 'frozen edit set hash is immutable' USING ERRCODE='55000';
          END IF;
          RETURN NEW;
        END IF;
        RAISE EXCEPTION 'invalid edit set transition' USING ERRCODE='23514';
      END; $$""")
    op.execute("CREATE TRIGGER edit_set_transition_guard BEFORE INSERT OR UPDATE ON edit_sets FOR EACH ROW EXECUTE FUNCTION guard_edit_set_transition()")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS guard_edit_set_transition()")
    op.execute("DROP FUNCTION IF EXISTS guard_patch_draft()")
    op.execute("DROP TABLE IF EXISTS verification_manifests,reviews,patches,edit_sets,data_corrections,fields CASCADE")
