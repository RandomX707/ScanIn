#!/bin/sh
# Single host-side entrypoint for starting the app and running real Playwright
# workflows. Run from macOS Terminal, outside the restricted Codex sandbox.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT"

export IDW_ENVIRONMENT=local
export IDW_AUTH_MODE=development
export IDW_STORAGE_ROOT="$ROOT/.runtime/document-storage"
export IDW_CLAMAV_SOCKET_PATH=/opt/homebrew/var/run/clamav/clamd.sock
export IDW_LOCAL_TENANT_ID=${IDW_E2E_TENANT_ID:-33333333-3333-4333-8333-333333333333}
export IDW_LOCAL_TENANT_NAME=${IDW_E2E_TENANT_NAME:-"Channel360 Browser E2E"}
export IDW_WEB_PORT=${IDW_WEB_PORT:-5174}
export IDW_WEB_BASE_URL="http://127.0.0.1:$IDW_WEB_PORT"

export PGHOST=127.0.0.1
export PGPORT=5432
export PGPASSFILE="${HOME:?HOME must be set}/.channel360-pgpass"
[ -r "$PGPASSFILE" ] || { echo "PGPASSFILE is not readable." >&2; exit 2; }

# start.sh verifies database ownership/credentials, bootstraps only the
# dedicated local demo tenant, and starts API/Vite. Reuse a healthy Channel360
# instance so this runner can be invoked against an already-running workbench.
if curl --silent --fail http://127.0.0.1:8000/health/ready >/dev/null \
  && curl --silent --fail "$IDW_WEB_BASE_URL/" >/dev/null; then
  echo "Using the ready Channel360 instance at $IDW_WEB_BASE_URL."
else
  env -u IDW_DATABASE_URL -u IDW_WORKER_DATABASE_URL -u IDW_MIGRATION_DATABASE_URL \
    -u IDW_DEVELOPMENT_AUTH_SECRET scripts/dev/start.sh
fi

.venv/bin/python scripts/host/create_browser_fixtures.py

if [ ! -x apps/web/node_modules/.bin/playwright ]; then
  env -u PGPASSFILE scripts/host/validate_channel360_e2e.sh browser-setup
fi

AUTH_SECRET_FILE=.runtime/dev/development-auth-secret
[ -r "$AUTH_SECRET_FILE" ] || { echo "Local development key was not created." >&2; exit 2; }
export IDW_DEVELOPMENT_AUTH_SECRET=$(cat "$AUTH_SECRET_FILE")
export TENANT_ID=$IDW_LOCAL_TENANT_ID
issue_token() {
  subject=$1 role=$2 tenant=${3:-$TENANT_ID}
  env -u PGPASSFILE .venv/bin/python scripts/issue_dev_token.py --tenant-id "$tenant" \
    --subject "$subject" --role "$role" --expires-in-seconds 3600
}
export IDW_DEMO_PROCESSOR_TOKEN=$(issue_token channel360-e2e-processor processor)
export IDW_DEMO_ADMIN_TOKEN=$(issue_token channel360-e2e-admin administrator)
export IDW_DEMO_REVIEWER_TOKEN=$(issue_token channel360-e2e-reviewer reviewer)
export IDW_DEMO_SELF_REVIEW_TOKEN=$(issue_token channel360-e2e-processor reviewer)
export IDW_DEMO_OTHER_TENANT_TOKEN=$(issue_token channel360-e2e-other-tenant processor \
  22222222-2222-4222-8222-222222222222)

env -u PGPASSFILE -u IDW_DEVELOPMENT_AUTH_SECRET scripts/host/validate_channel360_e2e.sh browser
curl --silent --fail http://127.0.0.1:8000/health/ready >/dev/null
curl --silent --fail "$IDW_WEB_BASE_URL/" >/dev/null
echo "Browser E2E completed. API and frontend remain running at http://127.0.0.1:8000 and $IDW_WEB_BASE_URL."
