# End-to-End Test Report

> Historical test snapshot. The latest executed results are in [LIVE_END_TO_END_TEST_REPORT.md](LIVE_END_TO_END_TEST_REPORT.md); read that report for current PASS and skip counts.

## Fresh local automated regression — 2026-11-08

```sh
.venv/bin/python -m pytest -q --junitxml=artifacts/autonomous-build/pytest-results.xml
```

Result: **146 passed, 20 skipped, 0 failed, 34 warnings**. JUnit: `artifacts/autonomous-build/pytest-results.xml`. Two added tests cover password prompt safety and guarded provisioning recovery. All 20 skips are PostgreSQL integration/security tests, marked `NOT_VERIFIED`; they are not counted as passing integration evidence.

Focused API/storage/frozen-plan run:

```sh
.venv/bin/python -m pytest tests/api/test_application.py tests/unit/test_immutable_storage.py tests/unit/test_frozen_editset_serialization.py -q
```

The full suite above is authoritative for current source. The new unauthenticated derived-download, edit-set-history, and audit route tests passed in isolation; CSV candidate tampering was blocked by its new verifier regression.

PostgreSQL-only execution:

```sh
.venv/bin/python -m pytest -m postgres -q -rs --junitxml=artifacts/autonomous-build/postgres-results.xml
```

The full suite reports **20 skipped** PostgreSQL tests; no real PostgreSQL test executed in this run. Isolated host provisioning/migration/test commands are prepared, but sandbox access to PostgreSQL remains unavailable. No database or role was modified.

Frontend:

```sh
npm run build --prefix apps/web
```

Result: **PASS**; TypeScript and Vite production build completed.

M0 preservation:

```sh
shasum -a 256 -c artifacts/m0/sha256.txt
```

Result: **5/5 PASS**.

Provisioning and launcher syntax:

```sh
sh -n scripts/host/provision_channel360_test_db.sh scripts/dev/start.sh scripts/dev/stop.sh scripts/dev/status.sh
.venv/bin/python -m pytest -q tests/unit/test_host_provisioning_safety.py
```

Result: shell syntax PASS; **2 focused tests passed**. This does not prove PostgreSQL DDL, role membership transitions, or migrations execute on the host.

Migration syntax:

```sh
.venv/bin/python -m alembic upgrade head --sql
```

Result: **PASS**, 369 SQL lines rendered offline. This is not a live migration.

## Workflow evidence boundary

- Unit/API tests demonstrate authentication boundaries, frozen-plan serialization integrity, storage staging/promotion, clean-scan requirements, and deterministic CSV/JSON/XLSX engines.
- `scripts/e2e_csv_release_demo.py` is a live-API exercise harness; it was compiled but not run because it needs PostgreSQL, ClamAV, host parser isolation and three valid tenant-scoped tokens.
- No browser automation run. The browser production bundle builds, but that does not demonstrate its API, database, scan, and download path end to end.
- No claim is made that a derived workbook/file has been released or downloaded from a live application.
- M0's manually supplied LibreOffice Calc 25.2.3.2 validation remains accepted evidence; no native spreadsheet test was repeated in this run.

The local launcher was invoked and failed closed because `IDW_DATABASE_URL` is not configured; no application process started. Runtime inspection found no ClamAV binaries/socket, Redis CLI, Playwright or Chromium. Host validation commands and exact NOT_VERIFIED blockers are recorded in [HOST_INTEGRATION_VALIDATION.md](HOST_INTEGRATION_VALIDATION.md). The Playwright workflow suite remains unexecuted; see [LIVE_END_TO_END_TEST_REPORT.md](LIVE_END_TO_END_TEST_REPORT.md).

## PostgreSQL regression-fix run — 2026-11-08

The host-provided first PostgreSQL JUnit remains preserved at `artifacts/host-integration/postgres-results.xml` and recorded 17 passed, 3 failed, 1 teardown error, 146 deselected. The test defects were corrected without changing the production frozen-patch guard or granting state transitions to the app role.

Current local run:

```sh
.venv/bin/python -m pytest -q --junitxml=artifacts/autonomous-build/pytest-results.xml
```

Result: **147 passed, 20 skipped, 0 failed, 34 warnings**. The 20 PostgreSQL tests are skipped in this environment, so this run does not verify the fixes against PostgreSQL. Focused available tests: **3 passed** (two provisioning tests and one transaction-scope test). No host database command was executed. The required host rerun is `PGPASSFILE="$HOME/.channel360-pgpass" scripts/host/validate_channel360_e2e.sh postgres`; do not run provisioning again.

Latest local run after adding a regression test for direct invocation of the ClamAV host validator: **148 passed, 20 skipped, 0 failed, 34 warnings**. The requested ClamAV action reached its configured socket but AF_UNIX connect was denied by the sandbox; it produced no scanner verdicts or ClamAV report.
