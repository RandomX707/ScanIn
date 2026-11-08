# M1 Tasks 1–2 Implementation Report

> Historical task report. Its earlier “NOT_VERIFIED” PostgreSQL status is superseded by 22 passing live integration tests on 2026-11-08. Overall M1 remains partial; see [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## PostgreSQL rerun remediation update — 2026-11-08

The historical report below predates the host provisioning and integration suite. The project owner supplied an initial PostgreSQL 16 JUnit result of 17 passed, 3 failed, 1 teardown error, and 146 deselected. See `docs/HOST_INTEGRATION_VALIDATION.md` for failure details and current status. Fixes were made to the tests/transaction boundary, not to grant the runtime role additional privileges. Current local full suite: 147 passed, 20 PostgreSQL tests skipped; the host integration result remains **NOT_VERIFIED pending rerun**.

Run against the already provisioned database and roles (do not provision again):

```sh
PGPASSFILE="$HOME/.channel360-pgpass" scripts/host/validate_channel360_e2e.sh postgres
```

Do not accept the database suite until the host JUnit confirms all 20 PostgreSQL tests passed with 0 failures, 0 errors, and 0 skips.

The next owner-supplied PostgreSQL run passed all 20 test bodies but had one teardown error. Its JUnit pinpointed the FK from derived `document_versions.edit_set_id` to `edit_sets`. Fixture cleanup has been corrected to remove only records belonging to its own two generated tenant UUIDs, after clearing the synthetic current-version, parent-version, and edit-set links in FK-safe order. It checks the actual catalog FK edges, verifies no tenant rows remain, and confirms all protected triggers are enabled after cleanup. No constraints or production triggers were removed. This fix is not yet live-verified; use the command above for the final rerun.

> **Historical report:** The Tasks 1–2 implementation evidence below records the original scope and environment. The repository has since been authorized through M7; later partial work and current regression evidence are maintained in `docs/AUTONOMOUS_BUILD_PROGRESS.md` and `artifacts/m1-task1-2/acceptance-results.json`.

## 2026-11-07 verification addendum

The latest full run is `.venv/bin/python -m pytest -q --junitxml=artifacts/m1-task1-2/pytest-results.xml`: **102 passed, 15 skipped, 0 failed**. The 48 M0 tests still pass. All 15 skipped tests require real PostgreSQL (12 M1 tenancy/audit, 2 document registry, 1 M2 edit-set guard); these remain NOT_VERIFIED. Frontend production build passed after the host-prepared lockfile and dependencies were available locally. `shasum -a 256 -c artifacts/m0/sha256.txt` verifies all five accepted M0 fixture hashes.

Subsequent M1 partial implementation added private streamed upload API routes, immutable local storage, scan orchestration, registry operations, bounded parser code, OS sandbox profiles, and a React upload/list/detail/original-download flow. M2 has a pure XLSX release workflow with a metadata-bound frozen edit-set hash, category policy, preflight, independent verification and default withholding of incomplete outputs, plus an append-only sidecar export model. Migration 0003 adds RLS-protected correction tables and draft/freeze mutation guards. The UI/API/database/scanner path is not end-to-end verified: PostgreSQL and ClamAV are unavailable to this sandbox, parser OS isolation has not been executed on the host, and no supported read-only workbook preview or Redis-backed worker/event pipeline exists yet. The migration has only been rendered offline. Uploads remain private until inspection is integrated and never claim readiness. See `docs/AUTONOMOUS_BUILD_PROGRESS.md` and `docs/M2_IMPLEMENTATION_REPORT.md` for status. These additions do not change the original Tasks 1–2 PostgreSQL acceptance result: NOT_VERIFIED.

**Scope:** Application foundation and tenant database/audit foundation only. No upload endpoints, file storage, document viewers, isolated parsing worker, or M2 behavior were implemented.

## Implementation

- FastAPI application factory, typed environment configuration, health/readiness responses, stable API error envelope, request logging that excludes headers/query/body, signed development identity restricted to local/test, and OIDC JWT validation configuration.
- React/TypeScript/Vite application shell and pinned package manifest.
- PostgreSQL runtime engine configuration, Alembic migration, tenant/user/file/document/version/audit tables, forced RLS with both `USING` and `WITH CHECK`, role bootstrap SQL, and tenant-filtered repository.
- Transaction-local tenant context helpers for validated API identities and validated worker job context. Repository tenant mismatch fails closed.
- Audit event serialization through a per-tenant locked head row, monotonic sequence, canonical JSON hashing, append-only application grants and DB trigger, chain verifier, and signed daily-digest interface.
- Database ingestion lifecycle has private staging, scan, quarantine, inspection, and ready states. Only `ready` is defined as viewable. Application role cannot advance this state; isolated worker transition permissions are deferred to Task 5.

## Verification results

The latest exact command `.venv/bin/python -m pytest -q --junitxml=artifacts/m1-task1-2/pytest-results.xml` completed with **63 passed, 12 skipped**. The original **48 M0 tests passed** without changes to their assertions. Ten API foundation tests and five new foundation unit tests passed. A separate `.venv/bin/python -m pytest -m postgres -q -rs` run reported **12 skipped, 63 deselected**; every PostgreSQL test explicitly skipped as `NOT_VERIFIED` because separate database URLs were not available.

The user reports that host PostgreSQL 16.15 (Homebrew) is running at `127.0.0.1:5432`. Local `psql` and `pg_isready` clients are also PostgreSQL 16.15 (Homebrew). The read-only connection attempt as `yashaswiram` failed with `Operation not permitted`; `pg_isready` from the sandbox returned `no response`. This is consistent with the earlier `initdb` `shmget: Operation not permitted` failure originating from Codex sandbox IPC/network restrictions, rather than evidence that the host service is stopped. The sandbox prevents confirming account privileges, existing databases/roles, or the server version through a live connection.

I requested normal host-execution escalation for a read-only connection/inventory check. The execution policy rejected escalation before prompting because `sandbox_approval` is disabled for this session. No database, role, permission, service, or system configuration was changed. No database mocks were substituted. RLS behavior, live migration execution, application credentials, cross-tenant adversarial tests, pooled context behavior, concurrent audit insertion, and DB tampering checks remain **NOT_VERIFIED** pending an execution environment permitted to connect to the reported host service.

Python 3.12.15 is used by the project virtual environment. No Valkey or Redis server was detected; the typed Redis-compatible queue URL is configuration only in Tasks 1–2, with queue connectivity and workers deferred to Task 5.

Node is `v22.23.3`, npm is `10.9.9`. The host-prepared `apps/web/package-lock.json` appeared in the shared workspace after the prior validation. `npm ci --offline --prefix apps/web` then installed 24 packages from the local cache and reported zero vulnerabilities. `npm run build --prefix apps/web` passed TypeScript compilation and Vite production bundling (Vite 8.3.4). A standard registry ping in the sandbox still returns `ENOTFOUND`; online installation was not repeated. Frontend build status is **PASS** based on the offline install and production build.

The accepted M0 SHA-256 manifest was rechecked after the test run: all five listed original/edited files match their recorded hashes.

The Alembic migration can be syntax-checked offline with `IDW_MIGRATION_DATABASE_URL=... .venv/bin/alembic upgrade head --sql`; that check does not prove live PostgreSQL behavior and is not counted as an integration pass.

## Required real PostgreSQL test setup

Use `scripts/host/postgres_preflight.sh` for read-only inventory in a host Terminal. Dedicated, parameterized test-role provisioning is prepared in `scripts/host/provision_channel360_test_db.sh`; it refuses pre-existing test database/role names and requires an explicit `CHANNEL360_PROVISION_TEST_DB=YES` operator opt-in. `scripts/host/run_channel360_postgres_tests.sh` applies Alembic and grants only to `channel360_test`, then runs all 12 PostgreSQL tests using separate migration/application credentials from a user-managed PGPASSFILE. Do not run any script against the `postgres` maintenance database for migrations or grants. The current sandbox cannot execute these host scripts.

## M1 requirement mapping

| Requirement | Tests/evidence | Status on this host |
| --- | --- | --- |
| CORE-3 tenant boundary | `tests/m1/test_tenant_rls.py`: cross-tenant read/insert, pooled context reset, no-context access, tenant policy/catalog checks | NOT_VERIFIED — all seven real DB tests skipped; requires PostgreSQL 16 and an isolated application role |
| CORE-4 audit history | `tests/m1/test_audit_chain.py`: concurrent sequence, append-only grants, owner modification/deletion detection, signed digest | NOT_VERIFIED — all five real DB tests skipped; requires PostgreSQL 16 and an isolated application role |
| SEC-2 worker tenant propagation | `test_worker_wrong_tenant_fails_closed`; pure unit `test_job_context_rejects_incorrect_document_tenant` | DB integration NOT_VERIFIED; queue/job authentication deferred to Task 5 |
| SEC-3 runtime credentials | `test_runtime_role_is_not_owner_or_rls_bypass`; role bootstrap SQL | NOT_VERIFIED — requires real PostgreSQL roles |
| Authenticated tenant required | API auth tests plus DB `test_missing_authenticated_tenant_context_has_no_access` | API unit PASS; DB enforcement NOT_VERIFIED |
| Task 1 API shell | `tests/api/test_application.py` (10 tests) | PASS |
| Task 1 web build | `npm ci --offline --prefix apps/web`; `npm run build --prefix apps/web` | PASS — installed 24 locked packages from cache; TypeScript and Vite build succeeded |
| M0 preservation | complete unchanged baseline suite | 48 PASS |

**M1 Tasks 1–2 acceptance decision: BLOCKED / NOT ACCEPTED.** Task 1 API tests and React production build pass. Task 2's required PostgreSQL security and audit tests have not executed. The user-reported server is available on the host, but this execution sandbox cannot connect and the approval policy prevents requesting host access.

Individual PostgreSQL tests include adversarial attempts for tenant A to read/insert tenant B data, context reuse through one pooled connection, mismatched worker tenant, owner/BYPASSRLS role, missing context, invalid initial `ready` state, missing `USING`/`WITH CHECK`/`FORCE`, concurrent audit writes, app UPDATE/DELETE, and privileged event modification/deletion detection.

## Security limitations

- Local development auth uses a signed shared secret and is not production identity management. Production must configure OIDC, TLS, managed secrets and suitable issuer/audience/JWKS.
- OIDC JWKS retrieval is synchronous in the request path at this foundation stage; production key caching and issuer integration need operational review.
- PostgreSQL roles/migration and grants are authored but not executed. The role and grant SQL now accept explicit role names; host scripts use dedicated `channel360_test_*` roles and a separate database, and stop if those names already exist.
- `psycopg`/`psycopg-binary` 3.3.8 is pinned; Psycopg uses LGPL-3.0-or-later. A complete dependency license scan has not run. Verify transitive license inventory before distribution.
- Daily signing is an interface plus local HMAC test signer. Production asymmetric signing and write-once storage delivery are not implemented.
- Job context validation helper is not a queue signature/authentication system. Secure worker envelopes and sandbox isolation are Task 5.
- Private staging/object access controls are modeled at the DB lifecycle level only. Upload, malware scan and object visibility enforcement belong to Tasks 3–4.

## Deferred scope

M1 Task 3 immutable object storage and upload lifecycle execution; Task 4 type detection, limits and malware scanning; Task 5 queue, worker isolation and event stream; Task 6 upload/list/read-only viewers; Task 7 corpus, benchmarks and milestone acceptance. Database audit integration for M0 XLSX edits remains a later integration milestone per accepted scope. No later M1 tasks or M2 work was started.

## Local commands

```sh
# API (local development secret required; database URL must use idw_app)
IDW_DEVELOPMENT_AUTH_SECRET='replace-with-at-least-32-random-bytes' \
IDW_DATABASE_URL='postgresql+psycopg://idw_app:...@127.0.0.1:5432/insurance_workbench' \
.venv/bin/uvicorn packages.api.app:create_app --factory --host 127.0.0.1 --port 8000

# API/foundation plus M0 tests
.venv/bin/python -m pytest -q --junitxml=artifacts/m1-task1-2/pytest-results.xml

# From a host Terminal only, first run the read-only inventory:
scripts/host/postgres_preflight.sh
# After reviewing output and approving the isolated test objects:
CHANNEL360_PROVISION_TEST_DB=YES scripts/host/provision_channel360_test_db.sh
# Set PGPASSFILE to a user-managed mode-0600 file outside this repository:
scripts/host/run_channel360_postgres_tests.sh

# Real PostgreSQL-only security/concurrency tests, after provisioning roles/migration
.venv/bin/python -m pytest -m postgres -q

# Frontend (after dependencies are available)
npm ci --prefix apps/web
npm run build --prefix apps/web

# Migrations (never use idw_app for these)
IDW_MIGRATION_DATABASE_URL='postgresql+psycopg://idw_migrator:...@127.0.0.1:5432/insurance_workbench' \
.venv/bin/alembic upgrade head
```
