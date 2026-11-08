#!/bin/sh
# Read-only PostgreSQL inventory. Safe to run against the maintenance DB.
set -eu

PGHOST=${PGHOST:-127.0.0.1}
PGPORT=${PGPORT:-5432}
PGDATABASE=postgres

command -v psql >/dev/null 2>&1 || { echo "psql is required" >&2; exit 127; }
psql -X -v ON_ERROR_STOP=1 -h "$PGHOST" -p "$PGPORT" -d "$PGDATABASE" -P pager=off <<'SQL'
SELECT version();
SELECT current_user,
       r.rolsuper AS is_superuser,
       r.rolcreatedb AS can_create_database,
       r.rolcreaterole AS can_create_roles
FROM pg_roles AS r
WHERE r.rolname = current_user;
SELECT datname,
       pg_get_userbyid(datdba) AS owner,
       shobj_description(oid, 'pg_database') AS channel360_marker
FROM pg_database
WHERE datname = 'channel360_test';
SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication,
       rolbypassrls, shobj_description(oid, 'pg_authid') AS channel360_marker
FROM pg_roles
WHERE rolname IN ('channel360_test_migrator', 'channel360_test_app', 'channel360_test_worker')
ORDER BY rolname;
SQL
