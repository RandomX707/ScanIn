#!/bin/sh
# Second, isolated stack for acceptance runs: API :8010, web :5180, own pid files and storage root.
# Leaves the normal dev stack (:8000/:5174) untouched.  Stop with scripts/dev/stop-test-stack.sh.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT"
RUNTIME="$ROOT/.runtime/test-stack"; mkdir -p "$RUNTIME"
for port in 8010 5180; do
  if curl --silent --max-time 1 "http://127.0.0.1:$port/" >/dev/null 2>&1; then echo "port $port already in use" >&2; exit 1; fi
done
PGHOST=${PGHOST:-127.0.0.1}; PGPORT=${PGPORT:-5432}; PGPASSFILE=${PGPASSFILE:-$HOME/.channel360-pgpass}
export PGHOST PGPORT PGPASSFILE
export IDW_DATABASE_URL="postgresql+psycopg://channel360_test_app@${PGHOST}:${PGPORT}/channel360_test"
export IDW_WORKER_DATABASE_URL="postgresql+psycopg://channel360_test_worker@${PGHOST}:${PGPORT}/channel360_test"
export IDW_MIGRATION_DATABASE_URL="postgresql+psycopg://channel360_test_migrator@${PGHOST}:${PGPORT}/channel360_test"
export IDW_CLAMAV_SOCKET_PATH=${IDW_CLAMAV_SOCKET_PATH:-/opt/homebrew/var/run/clamav/clamd.sock}
[ -S "$IDW_CLAMAV_SOCKET_PATH" ] || { echo "ClamAV socket unavailable" >&2; exit 1; }
export IDW_DEVELOPMENT_AUTH_SECRET=$(cat "$ROOT/.runtime/dev/development-auth-secret")
export IDW_LOCAL_TENANT_ID=44444444-4444-4444-8444-444444444444 IDW_LOCAL_TENANT_NAME="Channel360 Showcase"
export IDW_LOCAL_DEMO_TENANT_ID=$IDW_LOCAL_TENANT_ID IDW_LOCAL_DEMO_TENANT_NAME=$IDW_LOCAL_TENANT_NAME
export IDW_ENVIRONMENT=local IDW_AUTH_MODE=development IDW_LOCAL_DEMO_AUTH_ENABLED=true
export IDW_STORAGE_ROOT=${IDW_STORAGE_ROOT:-.runtime/test-stack/storage}
export IDW_WEB_ORIGINS='["http://127.0.0.1:5180"]'
mkdir -p "$IDW_STORAGE_ROOT"
nohup .venv/bin/uvicorn packages.api.app:create_app --factory --host 127.0.0.1 --port 8010 >"$RUNTIME/api.log" 2>&1 &
echo $! > "$RUNTIME/api.pid"
nohup env -u PGPASSFILE -u IDW_DATABASE_URL -u IDW_WORKER_DATABASE_URL -u IDW_MIGRATION_DATABASE_URL -u IDW_DEVELOPMENT_AUTH_SECRET \
  VITE_API_BASE_URL=http://127.0.0.1:8010 \
  sh -c 'cd "$1" && exec ./node_modules/.bin/vite --host 127.0.0.1 --port 5180 --strictPort' sh "$ROOT/apps/web" >"$RUNTIME/web.log" 2>&1 &
echo $! > "$RUNTIME/web.pid"
n=0; while [ $n -lt 40 ]; do
  curl -sf http://127.0.0.1:8010/health/ready >/dev/null 2>&1 && curl -sf http://127.0.0.1:5180/ >/dev/null 2>&1 && { echo "test stack ready: web :5180 api :8010"; exit 0; }
  n=$((n+1)); sleep 1; done
echo "test stack failed; see $RUNTIME/*.log" >&2; exit 1
