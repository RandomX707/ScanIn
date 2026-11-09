#!/bin/sh
# Safe macOS host validation. Commands are deliberately split: preflight is
# read-only; provision is guarded; validation touches only channel360_test.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT"
PGHOST=${PGHOST:-127.0.0.1}
PGPORT=${PGPORT:-5432}
PGPASSFILE=${PGPASSFILE:-"${HOME:?HOME must be set}/.pgpass"}
CLAMAV_SOCKET=${IDW_CLAMAV_SOCKET_PATH:-/opt/homebrew/var/run/clamav/clamd.sock}
ACTION=${1:-help}

usage() {
  cat <<'EOF'
Usage: scripts/host/validate_channel360_e2e.sh ACTION

  preflight      Read-only host/database/service inventory.
  provision      Guarded creation of channel360_test and three dedicated roles.
  postgres       Alembic + grants + all real PostgreSQL-marked tests.
  clamav         Real clean/EICAR/rejected/unavailable scanner checks.
  frontend       npm ci and production TypeScript/Vite build.
  browser-setup  Install the pinned temporary Playwright runner and Chromium.
  browser        Run real Chromium/API/PostgreSQL browser E2E tests.
  api-demo       Run the live API CSV demonstration script.
  validate       Run postgres, full Python suite, frontend, ClamAV and browser gates.

Provisioning requires CHANNEL360_PROVISION_TEST_DB=YES. Existing resources are
resumed only when their Channel360 marker and safe owner/role flags match.
Unmarked names are never altered. No action drops or resets a database.
EOF
}

preflight() {
  echo "== Channel360 host preflight (read-only) =="
  echo "Host: $PGHOST:$PGPORT; target DB: channel360_test"
  if command -v psql >/dev/null 2>&1; then psql --version; else echo "psql: MISSING"; fi
  if command -v pg_isready >/dev/null 2>&1; then pg_isready -h "$PGHOST" -p "$PGPORT" || true; fi
  echo "-- PostgreSQL inventory (read-only; connects to postgres maintenance DB) --"
  if ! scripts/host/postgres_preflight.sh; then
    echo "PostgreSQL preflight could not connect or inspect the cluster; no changes were made." >&2
  fi
  echo "-- Node/npm --"
  node --version 2>/dev/null || echo "node: MISSING"
  npm --version 2>/dev/null || echo "npm: MISSING"
  echo "-- Scanner --"
  command -v clamd || echo "clamd: MISSING"
  command -v clamscan || echo "clamscan: MISSING"
  command -v clamdscan || echo "clamdscan: MISSING"
  if [ -S "$CLAMAV_SOCKET" ]; then echo "ClamAV socket: PRESENT ($CLAMAV_SOCKET)"; else echo "ClamAV socket: NOT_FOUND ($CLAMAV_SOCKET)"; fi
  echo "-- Queue (not currently used by the synchronous ingestion path) --"
  command -v redis-cli || echo "redis-cli: MISSING/NOT_REQUIRED_FOR_CURRENT_SYNC_PATH"
  if command -v redis-cli >/dev/null 2>&1; then redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null || true; fi
  echo "-- Browser --"
  command -v chromium || command -v chromium-browser || command -v google-chrome || command -v safaridriver || echo "browser automation binary: MISSING"
  if [ -x apps/web/node_modules/.bin/playwright ]; then echo "Playwright package: PRESENT"; else echo "Playwright package: MISSING"; fi
  if [ -r apps/web/package-lock.json ]; then echo "Frontend lockfile: PRESENT"; else echo "Frontend lockfile: MISSING"; fi
}

provision() {
  if [ "${CHANNEL360_PROVISION_TEST_DB:-}" != "YES" ]; then
    echo "Set CHANNEL360_PROVISION_TEST_DB=YES only after reviewing the read-only preflight." >&2
    exit 2
  fi
  scripts/host/provision_channel360_test_db.sh
}

postgres() {
  if [ ! -r "$PGPASSFILE" ]; then
    echo "PGPASSFILE '$PGPASSFILE' is missing; create it outside the repo and chmod 600." >&2
    exit 2
  fi
  export PGPASSFILE PGHOST PGPORT
  scripts/host/run_channel360_postgres_tests.sh
}

clamav() {
  if [ ! -S "$CLAMAV_SOCKET" ]; then
    echo "NOT_VERIFIED: clamd Unix socket is unavailable: $CLAMAV_SOCKET" >&2
    exit 3
  fi
  .venv/bin/python scripts/host/validate_clamav.py --socket "$CLAMAV_SOCKET"
}

frontend() {
  npm ci --prefix apps/web
  npm run build --prefix apps/web
}

browser_setup() {
  # Keep the browser test runner out of the product dependency manifest. The
  # exact runner release is installed only into node_modules for host validation.
  (cd apps/web && npm install --no-save --package-lock=false @playwright/test@1.54.2)
  (cd apps/web && npx --no-install playwright install chromium)
}

browser() {
  WEB_URL=${IDW_WEB_BASE_URL:-http://127.0.0.1:${IDW_WEB_PORT:-5174}}
  if [ ! -x apps/web/node_modules/.bin/playwright ]; then
    echo "Run browser-setup first; it installs the pinned local Playwright runner and Chromium." >&2
    exit 2
  fi
  if ! curl --silent --show-error --fail http://127.0.0.1:8000/health/live >/dev/null; then
    echo "API is not reachable at http://127.0.0.1:8000; start it with docs/LOCAL_RUN_GUIDE.md." >&2
    exit 2
  fi
  if ! curl --silent --show-error --fail "$WEB_URL/" >/dev/null; then
    echo "Vite is not reachable at $WEB_URL; start it with docs/LOCAL_RUN_GUIDE.md." >&2
    exit 2
  fi
  for name in IDW_DEMO_OWNER_TOKEN IDW_DEMO_OTHER_TENANT_TOKEN; do
    eval "value=\${$name:-}"
    if [ -z "$value" ]; then echo "Missing $name in shell environment." >&2; exit 2; fi
  done
  mkdir -p artifacts/host-integration
  (cd apps/web && npx --no-install playwright test)
}

case "$ACTION" in
  preflight) preflight ;;
  provision) provision ;;
  postgres) postgres ;;
  clamav) clamav ;;
  frontend) frontend ;;
  browser-setup) browser_setup ;;
  browser) browser ;;
  api-demo) .venv/bin/python scripts/e2e_csv_release_demo.py ;;
  validate)
    postgres
    .venv/bin/python -m pytest -q --junitxml=artifacts/host-integration/pytest-results.xml
    frontend
    clamav
    browser_setup
    browser
    shasum -a 256 -c artifacts/m0/source-sha256.txt
    ;;
  *) usage; [ "$ACTION" = help ] || exit 2 ;;
esac
