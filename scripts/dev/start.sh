#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT"
RUNTIME="$ROOT/.runtime/dev"
API_PID="$RUNTIME/api.pid"
WEB_PID="$RUNTIME/web.pid"
mkdir -p "$RUNTIME"

fail() { echo "Cannot start Channel360: $*" >&2; exit 1; }
for exe in .venv/bin/uvicorn curl; do
  case "$exe" in
    .venv/*) [ -x "$exe" ] || fail "missing $exe; create the project virtual environment first" ;;
    *) command -v "$exe" >/dev/null 2>&1 || fail "missing executable: $exe" ;;
  esac
done
[ -x apps/web/node_modules/.bin/vite ] || fail "frontend dependencies are missing; run npm ci --prefix apps/web"
[ "${IDW_ENVIRONMENT:-local}" != production ] || fail "local launcher refuses production environment"

# The isolated test credentials are loaded by libpq from a mode-0600 file;
# passwords never enter database URLs, command arguments, or logs.
PGHOST=${PGHOST:-127.0.0.1}
PGPORT=${PGPORT:-5432}
PGPASSFILE=${PGPASSFILE:-${HOME:?HOME must be set}/.channel360-pgpass}
[ -r "$PGPASSFILE" ] || fail "PGPASSFILE is not readable: ${PGPASSFILE##*/}"
.venv/bin/python - "$PGPASSFILE" <<'PY' || fail "PGPASSFILE must be a regular file with no group/other permissions"
import os
import stat
import sys
mode = os.stat(sys.argv[1]).st_mode
if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
    raise SystemExit(1)
PY
export PGHOST PGPORT PGPASSFILE
export IDW_DATABASE_URL=${IDW_DATABASE_URL:-"postgresql+psycopg://channel360_test_app@${PGHOST}:${PGPORT}/channel360_test"}
export IDW_WORKER_DATABASE_URL=${IDW_WORKER_DATABASE_URL:-"postgresql+psycopg://channel360_test_worker@${PGHOST}:${PGPORT}/channel360_test"}
export IDW_MIGRATION_DATABASE_URL=${IDW_MIGRATION_DATABASE_URL:-"postgresql+psycopg://channel360_test_migrator@${PGHOST}:${PGPORT}/channel360_test"}
IDW_CLAMAV_SOCKET_PATH=${IDW_CLAMAV_SOCKET_PATH:-/opt/homebrew/var/run/clamav/clamd.sock}
export IDW_CLAMAV_SOCKET_PATH
[ -S "$IDW_CLAMAV_SOCKET_PATH" ] || fail "ClamAV socket is unavailable: $IDW_CLAMAV_SOCKET_PATH"

IDW_WEB_PORT=${IDW_WEB_PORT:-5173}
export IDW_WEB_PORT
for port in 8000 "$IDW_WEB_PORT"; do
  if curl --silent --max-time 1 "http://127.0.0.1:$port/" >/dev/null 2>&1; then
    fail "port $port already serves a process; run scripts/dev/status.sh and stop it explicitly"
  fi
done
[ ! -e "$API_PID" ] && [ ! -e "$WEB_PID" ] || fail "stale or active PID files exist; inspect scripts/dev/status.sh before removing"

# Persist only a local development signing key in the ignored runtime dir so
# separately issued development identities remain valid across restarts.
AUTH_SECRET_FILE="$RUNTIME/development-auth-secret"
if [ -z "${IDW_DEVELOPMENT_AUTH_SECRET:-}" ]; then
  if [ ! -f "$AUTH_SECRET_FILE" ]; then
    temporary_secret="$AUTH_SECRET_FILE.tmp.$$"
    (umask 077; .venv/bin/python -c 'import secrets; print(secrets.token_hex(32))' > "$temporary_secret")
    chmod 600 "$temporary_secret"
    mv "$temporary_secret" "$AUTH_SECRET_FILE"
  fi
  .venv/bin/python - "$AUTH_SECRET_FILE" <<'PY' || fail "local development auth key must have private file permissions"
import os
import stat
import sys
mode = os.stat(sys.argv[1]).st_mode
if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
    raise SystemExit(1)
PY
  IDW_DEVELOPMENT_AUTH_SECRET=$(cat "$AUTH_SECRET_FILE")
fi
[ "${#IDW_DEVELOPMENT_AUTH_SECRET}" -ge 32 ] || fail "IDW_DEVELOPMENT_AUTH_SECRET must contain at least 32 characters"
export IDW_DEVELOPMENT_AUTH_SECRET

if command -v pg_isready >/dev/null 2>&1; then
  pg_isready -h "$PGHOST" -p "$PGPORT" >/dev/null || fail "PostgreSQL is not accepting connections"
else
  fail "pg_isready is required for the database preflight"
fi
export IDW_LOCAL_TENANT_ID=${IDW_LOCAL_TENANT_ID:-44444444-4444-4444-8444-444444444444}
export IDW_LOCAL_TENANT_NAME=${IDW_LOCAL_TENANT_NAME:-"Channel360 Showcase"}
export IDW_LOCAL_DEMO_TENANT_ID=$IDW_LOCAL_TENANT_ID
export IDW_LOCAL_DEMO_TENANT_NAME=$IDW_LOCAL_TENANT_NAME
.venv/bin/python scripts/bootstrap_local_tenant.py \
  --tenant-id "$IDW_LOCAL_TENANT_ID" --name "$IDW_LOCAL_TENANT_NAME" >/dev/null \
  || fail "could not verify the isolated Channel360 local tenant"

export IDW_ENVIRONMENT=${IDW_ENVIRONMENT:-local}
export IDW_AUTH_MODE=${IDW_AUTH_MODE:-development}
if [ "$IDW_ENVIRONMENT" = local ] && [ "$IDW_AUTH_MODE" = development ]; then
  export IDW_LOCAL_DEMO_AUTH_ENABLED=${IDW_LOCAL_DEMO_AUTH_ENABLED:-true}
fi
export IDW_STORAGE_ROOT=${IDW_STORAGE_ROOT:-.runtime/document-storage}
export IDW_WEB_ORIGINS=${IDW_WEB_ORIGINS:-"[\"http://127.0.0.1:${IDW_WEB_PORT}\"]"}
mkdir -p "$IDW_STORAGE_ROOT"

WEB_URL="http://127.0.0.1:$IDW_WEB_PORT"

# No shell tracing: environment values and connection credentials stay private.
nohup .venv/bin/uvicorn packages.api.app:create_app --factory --host 127.0.0.1 --port 8000 \
  >"$RUNTIME/api.log" 2>&1 &
echo $! > "$API_PID"
nohup env -u PGPASSFILE -u IDW_DATABASE_URL -u IDW_WORKER_DATABASE_URL \
  -u IDW_MIGRATION_DATABASE_URL -u IDW_DEVELOPMENT_AUTH_SECRET \
  sh -c 'cd "$1" && exec ./node_modules/.bin/vite --host 127.0.0.1 --port "$2" --strictPort' \
  sh "$ROOT/apps/web" "$IDW_WEB_PORT" >"$RUNTIME/web.log" 2>&1 &
echo $! > "$WEB_PID"

attempt=0
while [ "$attempt" -lt 30 ]; do
  if curl --silent --fail http://127.0.0.1:8000/health/ready >/dev/null 2>&1 \
    && curl --silent --fail "$WEB_URL/" >/dev/null 2>&1; then
    echo "Channel360 is ready: $WEB_URL (API http://127.0.0.1:8000/docs)"
    exit 0
  fi
  attempt=$((attempt + 1))
  sleep 1
done
echo "Startup failed; inspect $RUNTIME/api.log and $RUNTIME/web.log (review logs for sensitive output before sharing)." >&2
"$ROOT/scripts/dev/stop.sh" >/dev/null 2>&1 || true
exit 1
