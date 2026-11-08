# Local Run Guide

Channel360 runs locally against PostgreSQL and a real ClamAV socket. See the top-level [README](../README.md) for installation, supported workflows and tests.

## Existing local development setup

1. Use the existing dedicated `channel360_test` database and Channel360 roles. If setting up a new development machine, first review the read-only `scripts/host/postgres_preflight.sh` and the guarded provisioner in `docs/LOCAL_RUN_GUIDE.md` history before provisioning. Never target a personal/shared database.
2. Store PostgreSQL credentials in a mode-0600 PGPASSFILE outside the repository. The launcher defaults to `~/.channel360-pgpass` and verifies permissions.
3. Start ClamAV with a reachable local socket. Set `IDW_CLAMAV_SOCKET_PATH` if it differs from the Homebrew default.
4. Install `.venv` dependencies (`requirements.txt` and, for synthetic PDF/DOCX acceptance fixtures, `requirements-test.txt`) and run `npm ci --prefix apps/web`.
5. Start with `IDW_WEB_PORT=5174 scripts/dev/start.sh`. It verifies the expected migration owner and idempotently bootstraps only the configured synthetic tenant in `channel360_test`.
6. Check `IDW_WEB_PORT=5174 scripts/dev/status.sh`. The UI is `http://127.0.0.1:5174`, the API is `http://127.0.0.1:8000`, and API docs are at `/docs`.

The normal demo tenant is `Channel360 Showcase`. Repeated E2E runs use the separate `Channel360 Browser E2E` tenant. Existing immutable data in the former demo tenant is retained; no reset is required.

Stop only Channel360 with `scripts/dev/stop.sh`. If PID state is stale, inspect the named process and listener before signaling it. Do not affect other applications or the service on port 5173.

The frontend local-demo sign-in requests short-lived server-issued identities and contains no signing secret. This API is permitted only in explicit local development mode from loopback clients. Do not reuse it in a production deployment.

## PDF and DOCX dependencies and runtime limits

PDF processing uses pinned `pypdf`, `pypdfium2`, and PDF.js dependencies plus the external `qpdf` executable. DOCX verification uses `lxml` and a per-job LibreOffice profile with macros set to Very High. On macOS, install qpdf and LibreOffice with `brew install qpdf libreoffice`.

The application requires the LibreOffice job to run inside its configured deny-by-default, no-network macOS sandbox. If that OS boundary refuses the job, DOCX native controls remain disabled and preflight stays in draft; no reviewer approval or derivative download is possible. Do not bypass the isolation to make rendering work. PDF remains available only for the supported unsigned AcroForm subset; encrypted, signed, certified and XFA files are rejected or read-only, and scanned pages have no OCR-derived edits.

Browser E2E fixtures are generated under ignored `.runtime/browser-fixtures` from synthetic data. Repeated runs use the dedicated `Channel360 Browser E2E` tenant and unique filenames; they do not reset or delete immutable records.
