# Host Integration Validation

Updated 2026-11-08. These commands target only the dedicated `channel360_test` database and synthetic test tenants. They do not drop or reset data.

## Current run evidence

See [LIVE_END_TO_END_TEST_REPORT.md](LIVE_END_TO_END_TEST_REPORT.md) for the exact run summary. The live PostgreSQL suite passed 22 tests, ClamAV passed 4 checks, Playwright passed 6 workflows, the frontend build passed, and the app URLs responded on ports 5174 and 8000.

## Prerequisites

- PostgreSQL 16 with the existing Channel360 test database and least-privilege roles.
- A mode-0600 PGPASSFILE stored outside this repository (defaults to `~/.channel360-pgpass`).
- A running ClamAV daemon and configured Unix socket.
- Python dependencies, Node dependencies and pinned Playwright/Chromium for browser runs.

The dev launcher checks its database owner and scanner. It fails closed if either service is unavailable. Do not put passwords in database URLs or print server-issued tokens.

## Run the suites

```sh
PGPASSFILE="$HOME/.channel360-pgpass" scripts/host/validate_channel360_e2e.sh postgres
scripts/host/validate_channel360_e2e.sh clamav
npm run build --prefix apps/web
shasum -a 256 -c artifacts/m0/source-sha256.txt
scripts/host/start_and_run_browser_e2e.sh
```

`start_and_run_browser_e2e.sh` starts the app on port 5174, uses the explicitly named `Channel360 Browser E2E` synthetic tenant, issues short-lived identities without printing them, runs CSV/JSON/XLSX, rejection, and formula-dependent browser tests, and leaves Channel360 running on success. It does not run against the primary showcase tenant. If the API or frontend ports are already occupied, inspect them and stop only a confirmed Channel360 process before using the launcher.

The standalone browser action can also use an already-running app with `IDW_WEB_BASE_URL=http://127.0.0.1:5174`; it requires short-lived tokens scoped to the test tenant in the current shell. Never paste those tokens into logs, screenshots or Git.

## Evidence handling

UI screenshots in `artifacts/ui-redesign/` are synthetic and credential-free. Generated Playwright traces/videos, JUnit results, downloads, storage, auth key and runtime logs are ignored by Git. Keep test artifacts local unless they have been reviewed and explicitly selected for publication.
