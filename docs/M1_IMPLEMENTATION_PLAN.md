# M1 Foundation Implementation Plan

> Historical implementation plan. Its authorization and verification status are superseded. Current scope and results are in the top-level README and `docs/LIVE_END_TO_END_TEST_REPORT.md`; M4–M7 are planned and were not implemented in this pass.
>
> **For agentic workers:** Use `superpowers:executing-plans` or the user-approved execution method. Work task by task and keep the M0 acceptance suite green. Do not commit or push without separate authorization.

**Goal:** Build the M1 application foundation for upload, immutable file storage and versions, inspection, tenancy, roles, isolated ingestion workers, malware scanning, an audit chain, document events and basic viewers.

**Architecture:** Keep the v2.1.1 modular monolith and separate ingestion, intelligence and export worker profiles. FastAPI and React provide the application surfaces; PostgreSQL owns tenant-scoped metadata and audit events; Redis backs jobs; local filesystem storage is used in development behind an object-store interface. The M0 XLSX core remains standalone and is called only for inspection in M1; native document editing and release are outside M1.

**Tech Stack:** Python 3.12; FastAPI; React; PostgreSQL 16; Redis-backed workers; local development storage and S3-compatible object storage interface; ClamAV through its socket; existing M0 `lxml`, `openpyxl`, and `pytest` pins.

**Spec:** `Insurance Document Workbench — System Design v2.1.1.md`, especially Security baseline, Data model, API, Frontend, Workers, Acceptance tests, Security tests and Milestones.

## Global Constraints

- Preserve the v2.1.1 architecture decisions; revise the architecture document only through an explicit architecture revision, never as an implementation shortcut.
- Every tenant-owned row has `tenant_id`; each tenant table uses RLS with both `USING` and `WITH CHECK`, and `FORCE ROW LEVEL SECURITY`.
- Migrations use an owner role; API and workers use a separate non-owner application role with `NOBYPASSRLS`.
- Set `SET LOCAL app.tenant_id` at the start of every tenant-scoped transaction, including worker transactions; repository filters complement RLS.
- Object keys are tenant-prefixed. Do not deduplicate content across tenants. Cross-tenant identifiers return 404.
- Detect file type from content; enforce size, page, ZIP expansion-ratio and nesting limits. Initial targets are 50 MB and 200 pages and must be benchmarked.
- Scan every upload with ClamAV and quarantine matches. Worker processes run non-root with per-job CPU, memory and time limits and no network access.
- Audit events are append-only; store the previous row hash and provide daily signed chain-head digest output to write-once storage. Application credentials have no UPDATE/DELETE grant on audit rows.
- Preserve immutable source bytes and source SHA-256. M1 creates no document-correction output or edit/release flow.
- Do not add OCR, Docling, LLMs, templates, CSV/JSON/PDF/DOCX editing, or M2 policy/release features in M1.
- Keep user supplied content untrusted; do not interpolate document content into logs, HTML or commands.
- Run all applicable M0 tests with every M1 test; no existing acceptance meaning may be weakened.
- Do not commit, push, or deploy without explicit user authorization.

## Review Focus

- Tenant context leaking between pooled API/worker transactions — test tenant A then B on the same connection and assert no cross-tenant rows or objects.
- Application code accidentally using a table-owner or bypass role — assert database role attributes and execute SEC-3 under application credentials.
- Partial upload, worker crash or retry leaving a visible incomplete document/version — inject interruption and assert deterministic cleanup/idempotency without adding M2 release semantics.
- Malformed/hostile archives exceeding decompression or nesting limits — fixtures must reject/quarantine within configured resource bounds and never crash a worker.
- Files for adapters not yet built being incorrectly labelled editable — assert explicit Read-only or Limited with `adapter_not_implemented` and preserve source SHA-256.

## Planned File Map

- Create `packages/api/` for FastAPI routing, request context and response contracts.
- Create `packages/storage/` for tenant-prefixed local/S3-compatible object interfaces and immutable file records.
- Create `packages/registry/` for documents, source versions, capability records and adapter lookup.
- Create `packages/ingestion/` for content-based type detection, ClamAV integration, limits and inspection pipeline.
- Create `packages/workers/` for Redis jobs, isolated worker profiles and stage events.
- Create `packages/audit/` for append-only events, chain verification and signed daily digests.
- Create `packages/db/` for PostgreSQL 16 connection setup, migrations, repositories and role bootstrap.
- Create `apps/web/` for the React upload/list/detail/viewer shell in the spec.
- Add `tests/m1/` unit, integration and security suites; grow `tests/corpus/` with expected-result fixtures.
- Add `docs/M1_IMPLEMENTATION_REPORT.md` and benchmark/security run reports after implementation.

## Tasks

### Task 1: Application and configuration foundation

**Files:** Create `packages/api/`, `packages/db/`, `packages/registry/`, `packages/storage/`, `packages/ingestion/`, `packages/workers/`, `packages/audit/`, and `apps/web/`; create local test/config entry points; update dependency pins only as needed.

**Interfaces:** Provide a FastAPI app factory and typed settings; React app shell; PostgreSQL 16 and Redis connection configuration; no secrets in source or test artifacts.

- [ ] Write smoke tests proving the API app constructs with test settings and the web shell builds.
- [ ] Run tests and confirm expected initial failures.
- [ ] Implement the minimal app/config skeleton using the established pinned stack.
- [ ] Verify API smoke tests, web build, and all M0 tests.

### Task 2: Tenant database, roles, and append-only audit foundation

**Files:** Create PostgreSQL migrations under `packages/db/migrations/`; tenant-scoped repositories under `packages/db/`; audit chain code under `packages/audit/`; tests in `tests/m1/test_tenant_rls.py` and `tests/m1/test_audit_chain.py`.

**Interfaces:** Migration/owner connection; application connection with no table ownership and `NOBYPASSRLS`; transaction helper that sets tenant context locally; repository filters; append-only `AuditEvent` creation and chain verifier.

- [ ] Write CORE-3, CORE-4, SEC-2 and SEC-3 tests using PostgreSQL 16 and the actual application role, including `USING`, `WITH CHECK`, and `FORCE ROW LEVEL SECURITY` assertions.
- [ ] Confirm tests fail against missing schema/security behavior.
- [ ] Implement tenant/user/document/file/version/audit tables and role bootstrap. Scope tenant columns and RLS to all tenant-owned tables.
- [ ] Implement repository tenant filters and transaction-local context for API and workers.
- [ ] Implement append-only previous-hash chain, tamper detection, and daily signed digest interface to write-once storage.
- [ ] Verify the four gates, plus M0, under application credentials. Test that migration/owner credentials are not used at runtime.

### Task 3: Immutable object storage, documents, and version registry

**Files:** Implement storage interfaces in `packages/storage/`; document/version registry in `packages/registry/`; migrations/repositories in `packages/db/`; API document routes in `packages/api/routes/documents.py`; tests in `tests/m1/test_storage_versions.py` and `tests/m1/test_document_api.py`.

**Interfaces:** `put_original(tenant_id, document_id, stream) -> StoredFile`; immutable file metadata with SHA-256/size/media type/key; original `SourceVersion`; document capability/status; local development backend and S3-compatible adapter boundary.

- [ ] Test streamed upload hashing, content-addressed per-tenant keys, immutable originals, version tree root creation and no cross-tenant dedupe.
- [ ] Test that rejected uploads create no visible version and that partial writes are cleaned up.
- [ ] Implement storage and registry operations without overwriting stored source bytes.
- [ ] Verify tenant-specific object access and 404 behavior for cross-tenant identifiers.

### Task 4: Ingestion inspection, file limits, and ClamAV quarantine

**Files:** Implement detection, quotas and inspection in `packages/ingestion/`; adapter registry in `packages/registry/`; scan/quarantine state in `packages/db/`; upload pipeline tests in `tests/m1/test_ingestion_security.py`.

**Interfaces:** `inspect_upload(stream, filename, tenant_id) -> IngestionResult`; result includes capability level, explicit reasons, detected content type, file facts and scan status. Unsupported adapters return Read-only or Limited with `adapter_not_implemented`.

- [ ] Write CORE-1 fixtures asserting unchanged original SHA-256, capability/reason, and unsupported-adapter downgrade.
- [ ] Write CORE-2 tests for ZIP bombs, encrypted packages, executable content, EICAR test signature and over-limit files; assert rejection/quarantine inside resource limits and no worker crash.
- [ ] Implement content-based type detection, allowlist checks, ZIP expansion/nesting checks, page/size limits and ClamAV socket scan.
- [ ] Ensure a match is quarantined and unavailable to normal viewers/workers; avoid logging content.
- [ ] Verify CORE-1 and CORE-2 plus M0.

### Task 5: Redis-backed isolated pipeline and event stream

**Files:** Implement job enqueue/claim/stage logic in `packages/workers/`; lifecycle events in `packages/audit/` or a separate event module; SSE route in `packages/api/routes/events.py`; tests in `tests/m1/test_worker_isolation.py` and `tests/m1/test_document_events.py`.

**Interfaces:** Typed job payload includes tenant/document/source-version ids and correlation id; job stages expose stable event names and timestamps; worker profile has explicit CPU, memory and time limits.

- [ ] Test tenant propagation and worker role/context under SEC-2, including cross-tenant object access denial.
- [ ] Test retry/idempotent ingestion and process interruption cleanup without adding M2 edit-release states.
- [ ] Implement non-root no-network worker execution for ingestion and stage events; keep intelligence/export profiles separate and unused unless needed for ingestion.
- [ ] Disable macros and use a fresh LibreOffice profile per job wherever LibreOffice is invoked.
- [ ] Verify event delivery, cleanup, isolation and M0.

### Task 6: React upload, document list, and read-only viewers

**Files:** Create `apps/web/` views and API client; FastAPI read routes in `packages/api/routes/`; frontend and API integration tests.

**Interfaces:** Show filename, source version, capability level/reason, processing state and available viewer. Every view identifies the version it displays. No correction or release controls are implemented in M1.

- [ ] Test upload/list/detail flows, capability reason display and viewer version identity.
- [ ] Test tenant separation at the API boundary and do not reveal cross-tenant existence.
- [ ] Build basic viewers from the backend preview manifest; unsupported formats remain explicit read-only/limited states.
- [ ] Verify production build and API integration tests with all earlier tests.

### Task 7: Corpus, benchmarks, security gates, and milestone close report

**Files:** Expand `tests/corpus/` to at least 50 synthetic or properly licensed producer files with expected JSON; add `tests/m1/test_core_acceptance.py`; write `docs/M1_IMPLEMENTATION_REPORT.md` and benchmark outputs.

- [ ] Run CORE-1 through CORE-4, SEC-2 and SEC-3 exactly as specified, under the application role for RLS tests.
- [ ] Benchmark size and page limits on the target machine; confirm or revise the working 50 MB/200 page limits with evidence.
- [ ] Run the complete prior milestone suite, verify original SHA-256 values, inspect quarantine/cleanup results, and run dependency license checks.
- [ ] Report each M1 gate PASS/FAIL/NOT_VERIFIED, security residuals, performance measurements, and any deployment-only controls.
- [ ] Stop before M2 features; do not add edit sets, document corrections, policy approval, or export/release routes.

## M1 Exit Criteria

- CORE-1, CORE-2, CORE-3, CORE-4, SEC-2 and SEC-3 pass without weakening prior gates.
- At least 50 producer-diverse, synthetic or properly licensed fixtures have expected-result JSON and source-hash preservation evidence.
- Ingestion is content-detected, size/page/ZIP bounded, ClamAV scanned and quarantines detections.
- Tenant isolation is exercised with the non-owner, `NOBYPASSRLS` application role; API and worker transactions establish tenant context locally.
- Audit tampering is detected; append-only grants and signed daily digest path are demonstrated. WORM-storage enablement is configured before production release.
- Size/page benchmark and license scan are recorded.
- M0's accepted XLSX-6 native-open result, 1×1-pixel image visual limitation, formula-dependent INCOMPLETE behavior, and M3 audit-event wiring deferral remain documented.

## Approval Boundary

This document records the original M1 plan and is not the current execution scope. The current task completes the M0–M3 stabilization baseline only; M4–M7 remain planned.
