#!/bin/sh
# Fully isolated Channel360 stack: its OWN PostgreSQL database, OWN storage root, OWN tenant, ports 8010/5180.
# Never touches channel360_test, .runtime/document-storage, :8000 or :5174.
#   isolated-stack.sh up      create db (if absent), migrate, grant, start api+web
#   isolated-stack.sh down    stop api+web only (database and storage are kept)
#   isolated-stack.sh db      create/migrate/grant the database only (no processes), e.g. for the PostgreSQL integration tests
#                             C360_ISO_WEB_MODE=preview serves a production build (no hot reload while tests run)
#   isolated-stack.sh status
#   isolated-stack.sh drop    drop THIS isolated database and storage (refuses any other name)
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT"
NAME=${C360_ISO_DB:-channel360_iso_dev}
case "$NAME" in channel360_iso_*) ;; *) echo "refusing database name '$NAME': must start with channel360_iso_" >&2; exit 2;; esac
STATE="$ROOT/.runtime/iso-stack"; mkdir -p "$STATE"; chmod 700 "$STATE"
API_PORT=${C360_ISO_API_PORT:-8010}; WEB_PORT=${C360_ISO_WEB_PORT:-5180}
case "$API_PORT:$WEB_PORT" in *:5174|8000:*) echo "refusing shared ports" >&2; exit 2;; esac
TENANT=${C360_ISO_TENANT:-bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb}
STORAGE="$STATE/storage"
PGHOST=${PGHOST:-127.0.0.1}; PGPORT=${PGPORT:-5432}
SRC_PGPASS=${C360_PGPASSFILE:-$HOME/.channel360-pgpass}
if [ "$NAME" = channel360_iso_dev ]; then PGPASSFILE="$STATE/pgpass"; else PGPASSFILE="$STATE/pgpass.$NAME"; fi   # never clobber another isolated database's credentials file
write_pgpass() {  # same role passwords, database field rewritten to the isolated name
  (umask 077; sed "s/:channel360_test:/:$NAME:/" "$SRC_PGPASS" > "$PGPASSFILE")
}
url() { echo "postgresql+psycopg://channel360_test_$1@${PGHOST}:${PGPORT}/$NAME"; }
provision_db() {
  EXISTS=$(psql -X -h "$PGHOST" -p "$PGPORT" -d postgres -Atc "select 1 from pg_database where datname='$NAME'")
  if [ -z "$EXISTS" ]; then
    psql -X -v ON_ERROR_STOP=1 -h "$PGHOST" -p "$PGPORT" -d postgres -q -c "GRANT channel360_test_migrator TO CURRENT_USER WITH SET TRUE" -c "CREATE DATABASE $NAME OWNER channel360_test_migrator" -c "COMMENT ON DATABASE $NAME IS 'channel360-isolated-dev-stack'"
    psql -X -h "$PGHOST" -p "$PGPORT" -d postgres -q -c "REVOKE channel360_test_migrator FROM CURRENT_USER" 2>/dev/null || true
  fi
  export IDW_MIGRATION_DATABASE_URL=$(url migrator)
  .venv/bin/alembic upgrade head >/dev/null
  psql -X -v ON_ERROR_STOP=1 -q -h "$PGHOST" -p "$PGPORT" -d "$NAME" -U channel360_test_migrator -v migration_role=channel360_test_migrator -v application_role=channel360_test_app -v worker_role=channel360_test_worker -f packages/db/grants.sql >/dev/null
  psql -X -v ON_ERROR_STOP=1 -q -h "$PGHOST" -p "$PGPORT" -d "$NAME" -U channel360_test_migrator -f packages/db/test_database_security.sql >/dev/null
  psql -X -v ON_ERROR_STOP=1 -q -h "$PGHOST" -p "$PGPORT" -d "$NAME" -U channel360_test_migrator -c "begin; select set_config('app.tenant_id','$TENANT',true); insert into tenants(id,name) values ('$TENANT','Isolated Dev') on conflict do nothing; commit;" >/dev/null
}

case "${1:-}" in
db)
  [ -r "$SRC_PGPASS" ] || { echo "missing $SRC_PGPASS" >&2; exit 1; }
  write_pgpass; export PGPASSFILE PGHOST PGPORT
  provision_db
  echo "database $NAME ready (migrated, granted, tenant $TENANT); no processes started" ;;
up)
  [ -r "$SRC_PGPASS" ] || { echo "missing $SRC_PGPASS" >&2; exit 1; }
  write_pgpass; export PGPASSFILE PGHOST PGPORT
  for port in $API_PORT $WEB_PORT; do curl --silent --max-time 1 "http://127.0.0.1:$port/" >/dev/null 2>&1 && { echo "port $port in use" >&2; exit 1; }; done
  provision_db
  [ -f "$ROOT/.runtime/dev/development-auth-secret" ] || { echo "missing dev auth secret" >&2; exit 1; }
  export IDW_DATABASE_URL=$(url app) IDW_WORKER_DATABASE_URL=$(url worker)
  export IDW_CLAMAV_SOCKET_PATH=${IDW_CLAMAV_SOCKET_PATH:-/opt/homebrew/var/run/clamav/clamd.sock}
  [ -S "$IDW_CLAMAV_SOCKET_PATH" ] || { echo "ClamAV socket unavailable" >&2; exit 1; }
  export IDW_DEVELOPMENT_AUTH_SECRET=$(cat "$ROOT/.runtime/dev/development-auth-secret")
  export IDW_LOCAL_TENANT_ID=$TENANT IDW_LOCAL_TENANT_NAME="Isolated Dev" IDW_LOCAL_DEMO_TENANT_ID=$TENANT IDW_LOCAL_DEMO_TENANT_NAME="Isolated Dev"
  export IDW_ENVIRONMENT=local IDW_AUTH_MODE=development IDW_LOCAL_DEMO_AUTH_ENABLED=true
  export IDW_STORAGE_ROOT=$STORAGE IDW_WEB_ORIGINS="[\"http://127.0.0.1:$WEB_PORT\"]"
  mkdir -p "$STORAGE"
  nohup .venv/bin/uvicorn packages.api.app:create_app --factory --host 127.0.0.1 --port "$API_PORT" >"$STATE/api.log" 2>&1 &
  echo $! > "$STATE/api.pid"
  if [ "${C360_ISO_WEB_MODE:-dev}" = preview ]; then
    # Production build served statically: editing frontend sources can no longer hot-reload (and so interrupt) a running browser test.
    (cd "$ROOT/apps/web" && env -u PGPASSFILE -u IDW_DATABASE_URL -u IDW_WORKER_DATABASE_URL -u IDW_MIGRATION_DATABASE_URL -u IDW_DEVELOPMENT_AUTH_SECRET \
      VITE_API_BASE_URL="http://127.0.0.1:$API_PORT" ./node_modules/.bin/vite build --outDir "$STATE/web-dist" --emptyOutDir >"$STATE/web-build.log" 2>&1) || { echo "web build failed; see $STATE/web-build.log" >&2; exit 1; }
    nohup env -u PGPASSFILE -u IDW_DATABASE_URL -u IDW_WORKER_DATABASE_URL -u IDW_MIGRATION_DATABASE_URL -u IDW_DEVELOPMENT_AUTH_SECRET \
      sh -c 'cd "$1" && exec ./node_modules/.bin/vite preview --outDir "$3" --host 127.0.0.1 --port "$2" --strictPort' sh "$ROOT/apps/web" "$WEB_PORT" "$STATE/web-dist" >"$STATE/web.log" 2>&1 &
  else
  nohup env -u PGPASSFILE -u IDW_DATABASE_URL -u IDW_WORKER_DATABASE_URL -u IDW_MIGRATION_DATABASE_URL -u IDW_DEVELOPMENT_AUTH_SECRET \
    VITE_API_BASE_URL="http://127.0.0.1:$API_PORT" \
    sh -c 'cd "$1" && exec ./node_modules/.bin/vite --host 127.0.0.1 --port "$2" --strictPort' sh "$ROOT/apps/web" "$WEB_PORT" >"$STATE/web.log" 2>&1 &
  fi
  echo $! > "$STATE/web.pid"
  n=0; while [ $n -lt 40 ]; do
    curl -sf "http://127.0.0.1:$API_PORT/health/ready" >/dev/null 2>&1 && curl -sf "http://127.0.0.1:$WEB_PORT/" >/dev/null 2>&1 && { echo "isolated stack ready: api :$API_PORT web :$WEB_PORT db $NAME tenant $TENANT"; exit 0; }
    n=$((n+1)); sleep 1; done
  echo "isolated stack failed; see $STATE/*.log" >&2; exit 1 ;;
down)
  for f in api web; do p="$STATE/$f.pid"; [ -f "$p" ] && kill "$(cat "$p")" 2>/dev/null || true; rm -f "$p"; done
  n=0; while [ $n -lt 15 ]; do
    curl --silent --max-time 1 "http://127.0.0.1:$API_PORT/" >/dev/null 2>&1 || curl --silent --max-time 1 "http://127.0.0.1:$WEB_PORT/" >/dev/null 2>&1 || break
    n=$((n+1)); sleep 1; done
  echo "isolated stack stopped (database $NAME and $STORAGE kept)" ;;
status)
  for port in $API_PORT $WEB_PORT; do printf "%s: " "$port"; curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:$port/" || true; done ;;
drop)
  write_pgpass; export PGPASSFILE PGHOST PGPORT
  [ "$(psql -X -h "$PGHOST" -p "$PGPORT" -d postgres -Atc "select shobj_description(oid,'pg_database') from pg_database where datname='$NAME'")" = "channel360-isolated-dev-stack" ] || { echo "database $NAME is not marked as an isolated dev stack; refusing" >&2; exit 1; }
  psql -X -h "$PGHOST" -p "$PGPORT" -d postgres -c "DROP DATABASE $NAME"
  case "$STORAGE" in "$ROOT"/.runtime/iso-stack/storage) rm -rf "$STORAGE";; esac
  rm -f "$PGPASSFILE" ;;
*) echo "usage: $0 up|down|status|drop" >&2; exit 2 ;;
esac
