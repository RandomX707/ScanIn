from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_role_passwords_use_psql_hidden_password_prompt_without_sql_literals():
    roles_sql = (ROOT / "packages/db/roles.sql").read_text(encoding="utf-8")

    assert "\\prompt -s" not in roles_sql
    assert "\\password" in roles_sql
    assert "PASSWORD :'" not in roles_sql
    assert "COMMENT ON ROLE" in roles_sql


def test_provisioning_recovery_is_marker_gated_and_never_drops_objects():
    provisioner = (ROOT / "scripts/host/provision_channel360_test_db.sh").read_text(encoding="utf-8")
    roles_sql = (ROOT / "packages/db/roles.sql").read_text(encoding="utf-8")

    assert "channel360-local-test-role-v1" in roles_sql
    assert "channel360-local-test-database-v1" in provisioner
    assert "BOOTSTRAP_CREATEDB" in provisioner
    assert "BOOTSTRAP_CREATEROLE" in provisioner
    assert "SET TRUE" in provisioner
    assert "SET FALSE" in provisioner
    assert "DROP DATABASE" not in provisioner.upper()
    assert "DROP ROLE" not in provisioner.upper()
