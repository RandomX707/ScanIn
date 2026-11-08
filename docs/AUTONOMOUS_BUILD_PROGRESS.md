# Channel360 Autonomous Build Progress

> Historical development snapshot. Its old authorization and test statuses are not current instructions or current evidence. See [LIVE_END_TO_END_TEST_REPORT.md](LIVE_END_TO_END_TEST_REPORT.md) and [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## Current verification snapshot

| Area | Status | Evidence |
| --- | --- | --- |
| M0 XLSX foundation | PASS | 48 M0 tests pass within the full suite; `shasum -a 256 -c artifacts/m0/sha256.txt`: 5/5 PASS; manually supplied LibreOffice Calc 25.2.3.2 validation remains recorded in M0 report |
| M1 Task 1 API/config | PASS (unit scope) | Full suite covers API identity, errors, readiness, request logging, upload auth and upload size limit |
| M1 Task 1 frontend | PASS | Locked dependencies already present; `npm run build --prefix apps/web` passed TypeScript and Vite 8.3.4 |
| M1 Task 2 DB/audit | IMPLEMENTED, NOT_VERIFIED | Migrations, separate migrator/application/orchestration-worker credentials, RLS repositories, transaction-local contexts and audit chain authored; live PostgreSQL tests skipped because sandbox TCP is prohibited |
| M1 Task 3 registry/storage | IMPLEMENTED, PARTIAL | Immutable local stage/promote/quarantine code; content-first registry now routes XLSX/CSV/JSON, identifies read-only PDF/image/DOCX/PPTX, detects suffix mismatch and rejects unsafe archive structures. Storage/registry DB integration remains skipped |
| M1 Task 4 ingestion | IMPLEMENTED, PARTIAL | Bounded upload pipeline, shallow format detection and fail-closed ClamAV protocol; real clamd service and complete hostile corpus not verified |
| M1 Task 5 queue/isolation/events | PARTIAL, NOT_VERIFIED | Restricted parser subprocess and separate least-privilege orchestrator DB role implemented; lifecycle updates require validated tenant job context. Tests inspect logic but do not execute DB role or OS isolation; Redis queue and worker event stream remain incomplete |
| M1 Task 6 web workflow | PARTIAL | React upload/list/detail, versioned CSV/JSON/XLSX viewer, target selection, data corrections, native edit/review/release controls, version history and audit history are implemented. Live DB and browser E2E remain unverified; no job progress stream exists |
| M1 Task 7 acceptance corpus | NOT_STARTED | Benchmark and milestone acceptance pack not complete |
| M2 correction/release core | IMPLEMENTED IN CODE, LIVE DB NOT_VERIFIED | Persisted edit-set APIs, frozen plans, policy, review, staging, independent verification, clean-candidate scan, version/manifest/audit transaction and VERIFIED-only download route are implemented; live PostgreSQL/ClamAV/browser execution remains outstanding |
| M3 XLSX application integration | IMPLEMENTED IN CODE, NOT LIVE-VERIFIED | Existing M0 workflow is selected through native release; formula-dependent outcomes remain INCOMPLETE/nondownloadable; live DB/scanner/browser integration is outstanding |
| M4–M7 | NOT_STARTED | Deterministic rules/templates, PDF, DOCX and optional plugin milestones remain |

### Latest automated evidence

- `.venv/bin/python -m pytest -q --junitxml=artifacts/autonomous-build/pytest-results.xml`: **144 passed, 20 skipped, 0 failed**, 34 warnings. All 20 skips are real PostgreSQL integration tests and are **NOT_VERIFIED**, not passes.
- `npm run build --prefix apps/web`: **PASS**, TypeScript and Vite production output.
- `shasum -a 256 -c artifacts/m0/sha256.txt`: **5/5 PASS**.
- `.venv/bin/python -m alembic upgrade head --sql`: **PASS**, 369 lines of offline SQL rendered; it is not a live migration.
- `.venv/bin/python -m pytest -m postgres -q -rs --junitxml=artifacts/autonomous-build/postgres-results.xml`: 20 skipped, 0 executed; NOT_VERIFIED.
- M0 SHA manifest: 5/5 PASS. Machine-readable result: `artifacts/autonomous-build/acceptance-results.json`.

## 2026-11-07 implementation update

- Added a content-first adapter registry and connected it to the restricted parser entrypoint. The parser reports media type, capability, adapter version, inspection features, extension mismatch findings and a confidence score; the original archive path/size/encryption/nesting defenses remain active.
- Added direct CSV and JSON byte-preserving adapters, frozen plans through the shared planner, and independent token/field verifiers. CSV supports UTF-8/BOM and Windows-1252, common delimited dialects, record-ending preservation and formula-injection rejection. JSON supports UTF-8 scalar JSON Pointer targets, duplicate-key refusal and local scalar replacement.
- Hardened text-verifier outcome policy: any failed check now blocks; unknown noncritical checks produce INCOMPLETE; unexpected permission operations or OOXML parts fail the frozen policy check.
- Added regression coverage for CSV/JSON content-first routing and suffix mismatch, while retaining the existing parser behavior for explicit read-only formats and unsafe ZIP members.
- Tests: **130 passed, 15 skipped**, 34 warnings. Skips remain PostgreSQL-only. Frontend build: PASS (TypeScript plus Vite). M0 SHA-256 manifest: 5/5 PASS.
- No PostgreSQL services/databases, unrelated repositories, external services, or M0 artifacts were modified.
- Remaining: no application API persistence for CSV/JSON edit plans/releases; no browser preview or target-selection UI; no successful scanner/host sandbox event lifecycle verification; no real DB tests. Continue with API-backed inspect/targets and correction workflow, then XLSX release integration.
- Added authenticated inspection that validates/hashes the tenant-scoped immutable original before invoking `RestrictedParser`. Lifecycle changes use a separate `idw_worker` credential and `ValidatedJobContext`; API credentials retain no ingestion-state update grant. If worker credentials or OS isolation are unavailable, inspection fails closed.
- Inspection optionally returns up to 2,000 typed targets. A processor can bind a target to the source fingerprint, persist append-only data corrections separately, see correction history and export a JSON sidecar. This workflow does not change the native document or create a derived version.
- Added isolated worker role bootstrap/grants and host test configuration; the worker role has only document-state column update plus audit-chain permissions and is NOBYPASSRLS. It has not been applied or tested against PostgreSQL.
- Latest full regression after worker-role and correction-path additions: **136 passed, 19 skipped, 0 failed**, 34 warnings. All 19 skips are PostgreSQL integration checks and NOT_VERIFIED. React production build passes. M0 workbook hashes remain 5/5.

## Host-only checks

User-reported host facts: PostgreSQL 16.15 listens on `127.0.0.1:5432`; Node 22.23.3 and npm 10.9.9 are installed; npm registry is reachable on the host. In this execution sandbox, local `psql` returned `Operation not permitted` and package-registry DNS was unavailable. Host execution escalation is disabled by policy. No PostgreSQL database, role, system service or permission has been changed, and sandbox-blocked host network checks will not be retried.

Use host Terminal only for database verification:

1. `scripts/host/postgres_preflight.sh` (read-only inventory).
2. Review output for collisions. The provisioner refuses existing names and uses only the isolated `channel360_test` database and dedicated migrator/application/worker roles.
3. After operator approval to create these isolated test objects: `CHANNEL360_PROVISION_TEST_DB=YES scripts/host/provision_channel360_test_db.sh`.
4. Create a mode-0600 `PGPASSFILE` outside this repository, then run `scripts/host/run_channel360_postgres_tests.sh`.
5. Do not run migrations or grants against the `postgres` maintenance database or any unrelated database.

The local frontend build is independently verified and does not require another registry probe. The parser network-isolation script is prepared but has **not** been run on the host: `.venv/bin/python scripts/host/verify_parser_boundary.py artifacts/m0/m0_original.xlsx`. It must pass on a non-root macOS user before claiming actual OS-level network denial.

## 2026-11-07 connected workflow update

- Added exact-version CSV/JSON/XLSX viewing and selectable target UI; linked targets to manual fields and displayed capability restrictions.
- Added edit-set listing/resumption, audit history API/UI, persistent category, typed patch, preflight/frozen plan, rebase and review API flow.
- Added release API: approval and frozen-plan integrity are required; immutable source bytes are patched by the native adapter and independently verified; candidate is privately staged and scanned; file/version/manifest/current pointer/audit are written atomically. Only VERIFIED derived files are downloadable; INCOMPLETE stays nondownloadable and BLOCKED produces no candidate.
- Fixed original retrieval to pin `kind='original'`, regardless of the current version pointer.
- Added live CSV workflow runner `scripts/e2e_csv_release_demo.py` and detailed `docs/LOCAL_RUN_GUIDE.md`. It includes unauthorized-role rejection and tampered-candidate blocking checks. It has not been run because it requires PostgreSQL, ClamAV, restricted host parsing and real identities.
- TDD missing-route tests demonstrated 404/405 before adding authenticated download/edit-set/audit endpoints; focused authentication tests pass afterward.
- Final code review found and fixed a stale-source mismatch in API preflight: the planner binds patch source identity to source SHA-256 while the frozen edit set independently binds the registry version UUID. Without the fix all API-preflighted patches would have been rejected as stale.
- Fresh full Python run: **144 passed, 20 skipped, 0 failed, 34 warnings**. React TS/Vite build PASS. M0 hashes 5/5 PASS. Alembic offline SQL: 369 lines rendered; not a live migration.
- No live database tests executed (20 remain NOT_VERIFIED); no ClamAV, browser E2E or OS-level parser boundary checks executed. The immediate download-through-browser success criterion is therefore not yet accepted.

Continue by running isolated host PostgreSQL tests and the live CSV demo, fixing any discovered defects, then implement the Redis job/event lifecycle and continue M4–M7. No permission was requested for routine coding decisions.

No Git initialization, commit, push, deployment, unrelated repository access, or architecture changes have been performed.

## 2026-11-07 host integration validation preparation

- Inspected the existing database provisioner, migration/grant runner, scanner protocol, local guide, and current environment. PostgreSQL client 16.15, Node 22.23.3, npm 10.9.9 are available. User-provided facts confirm host PostgreSQL and registry access. No ClamAV binaries/socket, Redis CLI, Chromium/Chrome or Playwright package are present; Safari driver is present but Safari automation remains unavailable.
- Did not retry the sandbox-blocked PostgreSQL network connection, provision any database/role, modify host services, install system software, or adjust host permissions.
- Added executable scripts/host/validate_channel360_e2e.sh with separate read-only preflight, collision-guarded provisioning, PostgreSQL migration/grant/test validation, real ClamAV/EICAR checks, frontend build, temporary pinned Playwright/Chromium setup, API/browser gates, and aggregate validation.
- Tightened the PostgreSQL runner to confirm exact test DB name, PostgreSQL major version, migrator ownership, and non-superuser/NOBYPASSRLS flags for the three dedicated roles before migrations. It writes isolated JUnit output. Fixed a malformed delimiter/owner assertion found during review.
- Added scripts/host/validate_clamav.py for clean, EICAR-infected, rejected-format and unavailable-scanner fail-closed checks. It has been compiled but cannot run without a real clamd socket.
- Added Playwright browser scenarios using actual API persistence for CSV, JSON and XLSX correction/review/verified download, plus formula-dependent XLSX INCOMPLETE/nondownloadable behavior. Added negative checks for self-approval, cross-tenant access, stale version, frozen-plan mutation, duplicate release idempotency, and original SHA preservation. The JS config/spec pass node --check; no browser execution is claimed.
- Added HOST_INTEGRATION_VALIDATION.md, LIVE_END_TO_END_TEST_REPORT.md, and host entrypoint instructions to LOCAL_RUN_GUIDE.md; updated limitation, remaining-work and implementation status reports.
- Fresh full regression: 144 passed, 20 skipped, 0 failed. Explicit PostgreSQL selection: 20 skipped, 144 deselected, 0 executed; skips remain NOT_VERIFIED. React TS/Vite build PASS. M0 hash manifest 5/5 PASS. Host script shell syntax, Python harness compile, JSON validity, Playwright JS syntax, and git diff --check pass.
- Overall connected upload-to-verified-download remains NOT_VERIFIED until the host sequence executes with isolated PostgreSQL credentials, real ClamAV, API/Vite servers and Playwright browser. No screenshots or live derivative outputs are claimed.

## 2026-11-08 PostgreSQL provisioning remediation and launch checks

- Fixed `packages/db/roles.sql`: removed invalid `\\prompt -s`; role passwords use psql `\\password` hidden prompts and do not appear in SQL, arguments, shell history, or normal output. Role creation remains transactionally marked and refuses unmarked collisions.
- Reworked `scripts/host/provision_channel360_test_db.sh` to support PostgreSQL 16 bootstrap accounts with `CREATEDB` + `CREATEROLE` (as well as superusers). It preflights exact role/database markers and flags, never drops or resets objects, temporarily enables `SET` membership only to create a database owned by the migrator, then restores `SET FALSE`; the exit trap attempts restoration after errors. Existing marked DB recovery only reapplies schema grants as the dedicated owner.
- Added two unit tests for hidden password handling, marker-gated recovery and absence of destructive DDL. Focused result: **2 passed**. `sh -n` passes for host provisioning and dev scripts.
- Added `scripts/dev/start.sh`, `stop.sh`, and `status.sh`. The launcher checks app configuration, database readiness and a real ClamAV socket, starts API/Vite only after prerequisites, polls health endpoints, and writes runtime state/logs under ignored `.runtime/dev`. It does not claim to start a Redis worker: the current ingestion path remains synchronous and Redis queue/event streaming is not implemented.
- Fresh full Python suite: **146 passed, 20 skipped, 0 failed**. All 20 skips require real PostgreSQL and remain NOT_VERIFIED. JUnit: `artifacts/autonomous-build/pytest-results.xml`.
- React TypeScript/Vite production build: **PASS**. M0 workbook manifest: **5/5 PASS**.
- Environment probe found `psql` 16.15 and `safaridriver`; clamd/clamscan/clamdscan, redis-cli, Playwright and Chromium are absent. No system packages, services, database objects or credentials were changed. PostgreSQL connectivity was not retried because the prior sandbox denial is unchanged.
- Attempted actual local start with `scripts/dev/start.sh`: stopped at fail-closed configuration check (`IDW_DATABASE_URL is not configured`); no process started. `scripts/dev/status.sh` reports API and Web stopped/unavailable. There is no running app URL to claim.
- Current M0 remains PASS. M1–M3 are implementation-present but not fully integration accepted; M4–M7 remain incomplete/not started. Overall product acceptance remains NOT_VERIFIED. No commits, pushes, deployments, or unrelated project changes were made.

## 2026-11-08 real PostgreSQL failure remediation

- Owner-supplied host JUnit recorded 17 passed, 3 failed, 1 teardown error, 146 deselected. The failures were: stale `DocumentRepository.advance_inspection`, two nested transaction-scope patterns, and cleanup attempting to delete frozen patches under `guard_patch_draft()`.
- Updated the data-correction integration test to transition document state only via `WorkerLifecycleRepository` on `worker_engine` within `worker_tenant_transaction`; no app-role privilege was added.
- Changed both affected worker/release tests to `engine.connect()` and let `tenant_transaction()` own their DB transaction. Added an idle-connection check to `tenant_transaction()` and extended pooled-connection regression assertions for commit, rollback, cleanup, and nested-use rejection.
- Hardened test fixture cleanup: it requires the exact `channel360_test` database and the database-owner identity, confirms protected triggers are enabled, disables them only in the transactional owner cleanup, re-enables them before commit, and checks their enabled state after cleanup. Frozen-patch production trigger behavior was not changed.
- Focused local tests: **3 passed**. Full local suite: **147 passed, 20 skipped, 0 failed**, 34 warnings. All 20 PostgreSQL tests remain NOT_VERIFIED here because no host DB test was run; the previous host JUnit remains preserved.
- Immediate host rerun: `PGPASSFILE="$HOME/.channel360-pgpass" scripts/host/validate_channel360_e2e.sh postgres`. Do not provision or recreate DB/roles. Goal is 20 passed, 0 failed, 0 errors, 0 skipped; no claim until that host execution completes.

## 2026-11-08 PostgreSQL teardown-order fix

- The subsequent host JUnit supplied by the owner reported 20 passing test bodies, 0 failures, and 1 teardown error (147 deselected): `document_versions_edit_set_fk` blocked deleting released edit sets while derived versions still referenced them.
- Inspected migrations `0001`–`0004`; cleanup now checks the live `pg_constraint` composite FK definitions and follows the actual edges, including both directions of derived-version/edit-set links, the document current-version pointer, parent-version chain, document/version cascade, and file references.
- In the existing owner-checked transaction, cleanup remains scoped to only the two UUID tenants created by each fixture. It removes child manifests/reviews/patches/corrections/fields/audit first; clears only those tenants' current-version and derived parent/edit-set pointers; deletes edit sets before documents; then relies on the existing document/version cascade and deletes files/users/tenant. FKs/RLS policies are not disabled or changed.
- Every non-internal trigger on the tenant-owned tables is checked enabled before cleanup; test-only cleanup disable/enable remains transactional, re-enables before commit, and verifies all application triggers are enabled afterward. A final transaction-local RLS check confirms zero rows remain for each fixture tenant. `test_approved_release_lineage_retry_and_fk_safe_cleanup` exercises cleanup with a released edit set, manifest, and derived version. Repeated suite runs use fresh UUID tenants and assert no residue.
- Local syntax, focused tests and full regression are being rerun. No host PostgreSQL rerun has occurred after this correction; preserve the current host JUnit and rerun using `PGPASSFILE="$HOME/.channel360-pgpass" scripts/host/validate_channel360_e2e.sh postgres` only. Do not provision/reset.

## 2026-11-08 ClamAV command execution

- Ran the requested `scripts/host/validate_channel360_e2e.sh clamav`. It first failed because direct invocation did not add the project root to `sys.path`; fixed the script and added an outside-repository `--help` invocation regression test.
- The rerun reached the configured socket path but the sandbox denied AF_UNIX connect with `PermissionError: [Errno 1] Operation not permitted`. No scanner version, clean/EICAR response, or scan report was produced; ClamAV remains NOT_VERIFIED. Run the same command from the user's macOS Terminal outside the Codex sandbox.
- Invocation test: 1 PASS. Full suite now **148 passed, 20 PostgreSQL skipped, 0 failed**; M0 workbook hashes remain 5/5 PASS.

## 2026-11-08 host service evidence and local app attempt

- Read the updated shared host artifacts: PostgreSQL JUnit is **20 tests, 0 failures, 0 errors, 0 skips**; ClamAV JSON is **4/4 PASS** with ClamAV 1.5.4. These close the host database and scanner test gates; the sandboxed pytest still skips its 20 DB tests.
- Confirmed `~/.channel360-pgpass` is mode 0600 without reading or printing its contents; the configured clamd socket exists with restrictive permissions. Host test evidence confirms those services/credentials work on macOS.
- Updated the dev launcher with defaults for existing `channel360_test` app/worker/migrator credentials via PGPASSFILE, the local ClamAV socket, and a mode-0600 git-ignored signing key. It bootstraps only the explicitly verified test database/migrator-owned local demo tenant. Vite receives neither PGPASSFILE nor DB URLs nor the signing key.
- Attempted `scripts/dev/start.sh` once with defaults: it failed at PostgreSQL readiness; explicit `pg_isready` returned `127.0.0.1:5432 - no response`. `scripts/dev/status.sh` confirms no API or Web processes. No DB writes occurred because tenant bootstrap follows readiness.
- Playwright/Chromium remain absent; no browser automation tool is available. Added `scripts/host/start_and_run_browser_e2e.sh` as one macOS Terminal action that starts the app, issues short-lived identities without printing them, installs the pinned browser runner if necessary, runs CSV/JSON/XLSX E2E, and verifies services remain healthy. It was not run in this sandbox.
- Latest local regression: 148 passed, 20 PostgreSQL skipped, 0 failed; all five M0 workbook hashes pass. Current frontend/backend URLs are not live in this execution.

## 2026-11-08 requested host-runner authorization

- Rechecked PostgreSQL with the existing PGPASSFILE and the configured clamd Unix socket once: PostgreSQL returned `Operation not permitted`; AF_UNIX returned `PermissionError: Operation not permitted`.
- Reviewed `scripts/host/start_and_run_browser_e2e.sh` and its invoked start/validation scripts. They are constrained to Channel360's existing test database/roles, local storage, synthetic demo tenant/data and local services; no provision/reset action is called. Hardened the wrapper to force local mode, the exact test DB role URLs, project-local storage, PGPASSFILE, and default local tenant, and to scrub DB credentials/auth signing key from browser/package processes.
- Requested escalated execution for the exact reviewed runner. The managed policy rejected the request before execution (`sandbox_approval: false`). No app/browser process was started and no browser dependency was installed.
- Status remains: host PostgreSQL 20/20 and ClamAV 4/4 PASS from shared host artifacts; browser E2E NOT_VERIFIED; no live URLs in this Codex process. One consolidated host command is `scripts/host/start_and_run_browser_e2e.sh`.
