#!/bin/sh
# Run the migration, grants, and the complete real PostgreSQL security suite
# only against the dedicated channel360_test database.
set -eu

PGHOST=${PGHOST:-127.0.0.1}
PGPORT=${PGPORT:-5432}
PGPASSFILE=${PGPASSFILE:-"$HOME/.pgpass"}
MIGRATOR=channel360_test_migrator
APPLICATION=channel360_test_app
WORKER=channel360_test_worker
DATABASE=channel360_test

if [ ! -r "$PGPASSFILE" ]; then
  echo "PGPASSFILE must be readable; keep it outside the repository and mode 0600." >&2
  exit 2
fi
export PGPASSFILE PGHOST PGPORT
DB_ID=$(psql -X -v ON_ERROR_STOP=1 -h "$PGHOST" -p "$PGPORT" -U "$MIGRATOR" -d "$DATABASE" \
  -Atc "SELECT current_database() || '|' || current_user || '|' || current_setting('server_version_num') || '|' || pg_get_userbyid((SELECT datdba FROM pg_database WHERE datname=current_database()))")
OLD_IFS=$IFS
IFS='|'
read -r DB_NAME DB_USER SERVER_VERSION DB_OWNER <<EOF
$DB_ID
EOF
IFS=$OLD_IFS
case "$SERVER_VERSION" in
  *[!0-9]*|'') echo "Refusing migration: invalid PostgreSQL server version '$SERVER_VERSION'." >&2; exit 1 ;;
esac
if [ "$DB_NAME" != "$DATABASE" ] || [ "$DB_USER" != "$MIGRATOR" ] || [ "$SERVER_VERSION" -lt 160000 ] || [ "$SERVER_VERSION" -ge 170000 ]; then
  echo "Refusing migration: expected dedicated PostgreSQL 16 database '$DATABASE' as '$MIGRATOR'; got '$DB_ID'." >&2
  exit 1
fi
if [ "$DB_OWNER" != "$MIGRATOR" ]; then
  echo "Refusing migration: '$DATABASE' is not owned by '$MIGRATOR'." >&2
  exit 1
fi
ROLE_FLAGS=$(psql -X -v ON_ERROR_STOP=1 -h "$PGHOST" -p "$PGPORT" -U "$MIGRATOR" -d "$DATABASE" \
  -Atc "SELECT count(*) FILTER (WHERE NOT rolsuper AND NOT rolbypassrls) FROM pg_roles WHERE rolname IN ('$MIGRATOR','$APPLICATION','$WORKER')")
if [ "$ROLE_FLAGS" != "3" ]; then
  echo "Refusing migration: the three dedicated roles must exist and have NOSUPERUSER/NOBYPASSRLS." >&2
  exit 1
fi

mkdir -p artifacts/host-integration
export IDW_MIGRATION_DATABASE_URL="postgresql+psycopg://${MIGRATOR}@${PGHOST}:${PGPORT}/${DATABASE}"
.venv/bin/alembic upgrade head
psql -X -v ON_ERROR_STOP=1 -h "$PGHOST" -p "$PGPORT" -U "$MIGRATOR" -d "$DATABASE" \
  -v migration_role="$MIGRATOR" -v application_role="$APPLICATION" \
  -v worker_role="$WORKER" \
  -f packages/db/grants.sql

export IDW_TEST_MIGRATION_DATABASE_URL="$IDW_MIGRATION_DATABASE_URL"
export IDW_TEST_APPLICATION_DATABASE_URL="postgresql+psycopg://${APPLICATION}@${PGHOST}:${PGPORT}/${DATABASE}"
export IDW_TEST_WORKER_DATABASE_URL="postgresql+psycopg://${WORKER}@${PGHOST}:${PGPORT}/${DATABASE}"
.venv/bin/python -m pytest -m postgres -q -rs --junitxml=artifacts/host-integration/postgres-results.xml
