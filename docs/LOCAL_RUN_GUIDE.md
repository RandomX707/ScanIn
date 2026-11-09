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

Optional AI explanations are disabled by default. To configure a provider locally, open the repository-root `.env` (create it with `touch .env && chmod 600 .env` if it does not exist), preserve all existing `IDW_*` settings, and append the following entries with values copied from the chosen provider's official API/model documentation:

```dotenv
CHANNEL360_LLM_PROVIDER=openai_compatible
CHANNEL360_LLM_API_KEY=<provider-secret-key>
CHANNEL360_LLM_MODEL=<exact-model-id>
CHANNEL360_LLM_BASE_URL=https://<provider-host>/<provider-api-base-path>
CHANNEL360_LLM_TIMEOUT_SECONDS=20
CHANNEL360_LLM_MAX_OUTPUT_TOKENS=500
```

Do not paste the key into chat, source code, frontend files or screenshots. Keep `.env` outside Git and never prefix these values with `VITE_`. The base URL must be the provider's documented HTTPS chat-completions base; Channel360 appends `/chat/completions` unless the URL already ends with that path. Some compatible services differ in support for JSON response mode; Channel360 retries without that one optional parameter only when the provider's 400 response explicitly identifies `response_format`, then still requires and validates a structured JSON response. Provider/model identifiers and endpoint values must come from the provider you choose; Channel360 does not assume or recommend one.

Optional bounds are `CHANNEL360_LLM_TIMEOUT_SECONDS` (1–60, default 20) and `CHANNEL360_LLM_MAX_OUTPUT_TOKENS` (64–2000, default 500). The endpoint must resolve only to public IP addresses; the HTTPS connection is pinned to those validated addresses while TLS still verifies the configured hostname. A user must explicitly click Generate, and the UI discloses that bounded extracted content is sent externally. Evidence IDs and locations are source-validated; text-overlap screening is only a heuristic and does not verify factual accuracy. No AI, parsing, edit, approval or release permissions depend on the provider.

Browser E2E fixtures are generated under ignored `.runtime/browser-fixtures` from synthetic data. Repeated runs use the dedicated `Channel360 Browser E2E` tenant and unique filenames; they do not reset or delete immutable records.
