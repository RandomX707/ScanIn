"""Allow an explicit, structurally verified DOCX release outcome."""
from alembic import op

revision = "0005_docx_verification_profiles"
down_revision = "0004_native_release"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE verification_manifests DROP CONSTRAINT verification_manifests_outcome_check")
    op.execute("""ALTER TABLE verification_manifests ADD CONSTRAINT verification_manifests_outcome_check
      CHECK(outcome IN ('VERIFIED','STRUCTURALLY_VERIFIED','INCOMPLETE','BLOCKED'))""")
    op.execute("""ALTER TABLE verification_manifests ADD CONSTRAINT structural_docx_manifest_contract
      CHECK(outcome <> 'STRUCTURALLY_VERIFIED' OR
        (adapter='docx' AND manifest->>'verification_profile'='structural' AND
         manifest->>'structural_checks'='PASS' AND manifest->>'visual_checks'='NOT_PERFORMED'))""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM verification_manifests WHERE outcome='STRUCTURALLY_VERIFIED') THEN
        RAISE EXCEPTION 'cannot downgrade while structurally verified DOCX release records exist';
      END IF;
    END $$""")
    op.execute("ALTER TABLE verification_manifests DROP CONSTRAINT structural_docx_manifest_contract")
    op.execute("ALTER TABLE verification_manifests DROP CONSTRAINT verification_manifests_outcome_check")
    op.execute("""ALTER TABLE verification_manifests ADD CONSTRAINT verification_manifests_outcome_check
      CHECK(outcome IN ('VERIFIED','INCOMPLETE','BLOCKED'))""")
