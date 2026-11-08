#!/bin/sh
# Guarded, resumable provisioning for Channel360's isolated local test DB.
# Never drops or resets any database or role. Cluster role names are protected
# by role comments; the database has a separate database comment marker.
set -eu

if [ "${CHANNEL360_PROVISION_TEST_DB:-}" != "YES" ]; then
  echo "Refusing DDL. Review the read-only preflight, then explicitly set CHANNEL360_PROVISION_TEST_DB=YES." >&2
  exit 2
fi

PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-5432}"
MIGRATOR=channel360_test_migrator
APPLICATION=channel360_test_app
WORKER=channel360_test_worker
DATABASE=channel360_test
DATABASE_MARKER=channel360-local-test-database-v1
PREFLIGHT_FILE="${TMPDIR:-/tmp}/channel360-test-preflight.$$"
BOOTSTRAP_USER=""
TEMP_SET_GRANT=0
cleanup() {
  if [ "$TEMP_SET_GRANT" = 1 ] && [ -n "$BOOTSTRAP_USER" ]; then
    psql -X -v ON_ERROR_STOP=1 -h "$PGHOST" -p "$PGPORT" -d postgres \
      -v bootstrap_user="$BOOTSTRAP_USER" >/dev/null <<'SQL' || true
GRANT channel360_test_migrator TO :"bootstrap_user" WITH SET FALSE;
SQL
  fi
  rm -f "$PREFLIGHT_FILE"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

command -v psql >/dev/null 2>&1 || { echo "psql is required" >&2; exit 127; }
command -v createdb >/dev/null 2>&1 || { echo "createdb is required" >&2; exit 127; }

# One read-only query determines whether names are free or carry our explicit
# markers. Unmarked objects are never modified, even when their names match.
psql -X -v ON_ERROR_STOP=1 -h "$PGHOST" -p "$PGPORT" -d postgres -AtF '|' <<'SQL' > "$PREFLIGHT_FILE"
SELECT
  (current_setting('server_version_num')::integer BETWEEN 160000 AND 169999),
  (SELECT rolsuper FROM pg_roles WHERE rolname=current_user),
  (SELECT rolcreatedb FROM pg_roles WHERE rolname=current_user),
  (SELECT rolcreaterole FROM pg_roles WHERE rolname=current_user),
  EXISTS (SELECT 1 FROM pg_database WHERE datname='channel360_test'),
  (SELECT count(*) FROM pg_roles WHERE rolname IN ('channel360_test_migrator','channel360_test_app','channel360_test_worker')),
  (SELECT count(*) FROM pg_roles WHERE rolname IN ('channel360_test_migrator','channel360_test_app','channel360_test_worker')
    AND coalesce(shobj_description(oid,'pg_authid'),'')='channel360-local-test-role-v1'
    AND rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
    AND NOT rolreplication AND NOT rolbypassrls),
  coalesce((SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='channel360_test'),''),
  coalesce((SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname='channel360_test'),'');
SQL
IFS='|' read -r SERVER16 BOOTSTRAP_SUPERUSER BOOTSTRAP_CREATEDB BOOTSTRAP_CREATEROLE DATABASE_EXISTS ROLE_COUNT MARKED_ROLE_COUNT DATABASE_OWNER DATABASE_COMMENT < "$PREFLIGHT_FILE"
if [ "$SERVER16" != "t" ] || { [ "$BOOTSTRAP_SUPERUSER" != "t" ] && { [ "$BOOTSTRAP_CREATEDB" != "t" ] || [ "$BOOTSTRAP_CREATEROLE" != "t" ]; }; }; then
  echo "Requires PostgreSQL 16 and a superuser or CREATEDB+CREATEROLE bootstrap account; no changes made." >&2
  exit 1
fi

if [ "$DATABASE_EXISTS" = "t" ]; then
  if [ "$DATABASE_OWNER" != "$MIGRATOR" ] || [ "$DATABASE_COMMENT" != "$DATABASE_MARKER" ] \
      || [ "$ROLE_COUNT" != "3" ] || [ "$MARKED_ROLE_COUNT" != "3" ]; then
    echo "channel360_test exists without the complete Channel360 ownership markers; refusing modification." >&2
    echo "Inspect database owner/comment and role markers manually. No objects were changed." >&2
    exit 1
  fi
  # A previous interruption may have left only the temporary SET membership
  # enabled. Restore its least-privilege state before returning.
  psql -X -v ON_ERROR_STOP=1 -h "$PGHOST" -p "$PGPORT" -d postgres \
    -v bootstrap_user="$(psql -X -h "$PGHOST" -p "$PGPORT" -d postgres -Atc 'SELECT current_user')" >/dev/null <<'SQL'
GRANT channel360_test_migrator TO :"bootstrap_user" WITH SET FALSE;
SQL
  echo "Resuming only the marked Channel360 database: reapplying idempotent schema grants."
  psql -X -v ON_ERROR_STOP=1 -h "$PGHOST" -p "$PGPORT" -U "$MIGRATOR" -d "$DATABASE" \
    -f packages/db/test_database_security.sql
  echo "The marked Channel360 test database already exists. Run scripts/host/run_channel360_postgres_tests.sh."
  exit 0
fi

if [ "$ROLE_COUNT" != "$MARKED_ROLE_COUNT" ]; then
  echo "One or more expected role names exist without the exact Channel360 marker/least-privilege flags." >&2
  echo "Refusing to alter ambiguous cluster roles; no changes were made." >&2
  exit 1
fi

echo "Provisioning only $DATABASE and the three marked Channel360 roles."
echo "Each role password is entered twice through psql's hidden password prompt."
psql -X -v ON_ERROR_STOP=1 -h "$PGHOST" -p "$PGPORT" -d postgres \
  -v migration_role="$MIGRATOR" -v application_role="$APPLICATION" -v worker_role="$WORKER" \
  -f packages/db/roles.sql

# PostgreSQL 16 creates CREATEROLE memberships with SET FALSE by default.
# Temporarily enable SET only for the bootstrap identity so it can create the
# database with the dedicated migrator as owner. A trap restores SET FALSE.
BOOTSTRAP_USER="$(psql -X -v ON_ERROR_STOP=1 -h "$PGHOST" -p "$PGPORT" -d postgres -Atc 'SELECT current_user')"
TEMP_SET_GRANT=1
psql -X -v ON_ERROR_STOP=1 -h "$PGHOST" -p "$PGPORT" -d postgres \
  -v bootstrap_user="$BOOTSTRAP_USER" >/dev/null <<'SQL'
GRANT channel360_test_migrator TO :"bootstrap_user" WITH ADMIN TRUE, INHERIT FALSE, SET TRUE;
SQL

# createdb accepts DESCRIPTION as the database comment. If the process stops
# after CREATE DATABASE but before its comment is visible, rerun refuses and
# requires manual ownership review rather than guessing.
createdb -h "$PGHOST" -p "$PGPORT" -O "$MIGRATOR" "$DATABASE" "$DATABASE_MARKER"
psql -X -v ON_ERROR_STOP=1 -h "$PGHOST" -p "$PGPORT" -d postgres \
  -v bootstrap_user="$BOOTSTRAP_USER" >/dev/null <<'SQL'
GRANT channel360_test_migrator TO :"bootstrap_user" WITH SET FALSE;
SQL
TEMP_SET_GRANT=0
psql -X -v ON_ERROR_STOP=1 -h "$PGHOST" -p "$PGPORT" -d "$DATABASE" \
  -U "$MIGRATOR" \
  -f packages/db/test_database_security.sql

cat <<'EOF'
Provisioning complete. Passwords were collected interactively and were not written to the repository.

Create a mode-0600 PGPASSFILE outside the repository with the three role passwords.
Use only the isolated database channel360_test. Then run:
  PGPASSFILE="$HOME/.pgpass" scripts/host/run_channel360_postgres_tests.sh
EOF
