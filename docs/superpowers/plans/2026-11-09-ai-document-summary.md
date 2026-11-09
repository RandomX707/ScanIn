# AI Document Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit AI explanation and comparison modes to the document Summary tab without changing document workflow permissions.

**Architecture:** Reuse exact-version tenant authorization and the restricted parser, adding a bounded evidence-only parser result used only by an explicit AI endpoint. A configurable backend provider makes one HTTPS OpenAI-compatible request; the UI keeps results in session memory keyed by document/version.

**Tech Stack:** FastAPI, Pydantic settings, existing restricted parser, pinned HTTPX, React/TypeScript, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-09-ai-document-summary-design.md`

## Global Constraints
- No AI is default and makes no provider calls.
- AI requests require an explicit user action and exact authorized immutable source version.
- API credentials remain server-side and are never logged or returned.
- External calls use configured HTTPS only, no redirects, bounded payload/output/time.
- Provider output is informational and cannot affect edits, authorization, approvals, verification or release.
- Synthetic data only for provider integration tests; mocks for ordinary tests.

## Review Focus
- Cross-tenant or stale-version access: endpoint tests must assert 404/409 without invoking provider.
- Prompt injection and fabricated evidence IDs: provider tests must show document instructions remain untrusted and unsupported references are rejected.
- Provider failure and missing credentials: No AI response remains available; AI returns safe bounded error.
- Switching document/version/logout: UI clears or isolates cached response.
- Scanned PDF with no text: evidence bundle says OCR is unavailable and does not invent content.

### Task 1: Bounded parser evidence
**Files:** Modify `packages/workers/parse_entry.py`, `packages/workers/parser.py`, `packages/core/adapters/docx/adapter.py`; Test `tests/unit/test_sandboxed_parser.py`, `tests/unit/test_pdf_docx_parser.py`.
- [x] Write tests for bounded evidence across CSV/JSON/XLSX/PDF/DOCX and scanned PDF limitation.
- [x] Run tests and confirm expected failures.
- [x] Add opt-in `include_summary_evidence` that emits server-created references and bounded excerpts without changing normal inspection output.
- [x] Run parser tests.

### Task 2: Provider and configuration
**Files:** Create `packages/core/summaries/llm.py`; Modify `packages/api/settings.py`; Test `tests/unit/test_llm_summary.py`.
- [x] Test disabled provider, HTTPS/SSRF endpoint validation, valid structured output, invalid JSON/evidence refs, injection handling and provider errors with mocked HTTP transport.
- [x] Confirm tests fail before implementation.
- [x] Implement OpenAI-compatible adapter with SecretStr, strict limits, no redirects/proxies, bounded timeout and evidence-linked claims.
- [x] Run provider/settings tests.

### Task 3: Tenant-scoped endpoint
**Files:** Modify `packages/api/documents.py`, `packages/api/app.py`; Test `tests/api/test_ai_summary.py`.
- [x] Test explicit endpoint source binding, auth, tenant isolation, source hash, stale/missing versions, missing key and provider failures.
- [x] Confirm tests fail before implementation.
- [x] Add authenticated exact-version endpoint; source bytes and evidence come from storage and restricted parser only; inject provider in app state.
- [x] Run API tests.

### Task 4: Summary UI
**Files:** Modify `apps/web/src/App.tsx`, `apps/web/src/style.css`; Test `apps/web` summary component/browser tests.
- [x] Add tests for default No AI/no request, explicit generation, compare without generation, cache across toggles, and no cross-document leakage.
- [x] Confirm tests fail before implementation.
- [x] Add segmented toggle, disclosure, AI response/evidence/limitations/model, compare layout and safe error/loading states.
- [x] Run frontend tests/build and targeted Playwright.

### Task 5: Docs and regression
**Files:** Modify README/config example/feature matrix and relevant API docs.
- [x] Document all `CHANNEL360_LLM_*` variables with placeholders and external data boundaries.
- [x] Run Python, PostgreSQL, ClamAV, Playwright, PDF corpus, M0, parser isolation and frontend build checks.
- [ ] Commit and push to `feat/pdf-docx-support`; update draft PR #1 without merging.
