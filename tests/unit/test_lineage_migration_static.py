"""Static guards for migration 0008. Behaviour is verified on real PostgreSQL by scripts/dev/lineage_migration_check.py
(FORCE ROW LEVEL SECURITY hid every row from the migration role, so an unguarded backfill silently changed nothing)."""
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[2] / "packages/db/migrations/versions/0008_version_lineage_class.py").read_text()


def test_backfill_lifts_force_rls_only_for_the_migration_and_restores_it():
    assert SOURCE.count('ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY') == 1 and SOURCE.count('ALTER TABLE {table} FORCE ROW LEVEL SECURITY') == 1
    assert SOURCE.index("NO FORCE ROW LEVEL SECURITY") < SOURCE.index("UPDATE document_versions SET lineage_class='original'")
    assert SOURCE.index('ALTER TABLE {table} FORCE ROW LEVEL SECURITY') > SOURCE.index("lineage backfill incomplete")


def test_backfill_cannot_silently_do_nothing():
    assert "lineage backfill incomplete" in SOURCE and "kind='original' AND lineage_class<>'original'" in SOURCE


def test_unknown_is_a_root_property_not_a_sibling_snapshot_read():
    assert "unknown_roots" in SOURCE and "unknown_descendants" in SOURCE and "v.edit_set_id IS NULL" in SOURCE


def test_migration_fails_fast_instead_of_queueing_behind_long_transactions():
    assert "SET LOCAL lock_timeout" in SOURCE and SOURCE.index("SET LOCAL lock_timeout") < SOURCE.index("NO FORCE ROW LEVEL SECURITY")
