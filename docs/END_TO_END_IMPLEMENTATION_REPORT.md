# End-to-End Implementation Report

> Historical implementation snapshot dated 2026-11-07. Its unverified status is superseded by the 2026-11-08 live results in [LIVE_END_TO_END_TEST_REPORT.md](LIVE_END_TO_END_TEST_REPORT.md). Product limitations remain in [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## Connected functionality implemented

- Accepted M0 XLSX native edit engine, frozen plan, independent verifier, adversarial tests, and artifacts preserved.
- Authenticated tenant-scoped document list, upload registration after clean scan, worker-role lifecycle inspection, source-version viewer for CSV/JSON/XLSX, target selection, capability restrictions, and linked field list.
- Manual field discovery, append-only data correction and audit, history, sidecar JSON export.
- Administrator-only document category, persistent draft edit sets and typed patches, fingerprint re-resolution, preflight/frozen plan persistence, rebase support, reviewer approval with author/reviewer separation, frozen-plan deserialization integrity checks.
- API release endpoint requires approval and unchanged base version, applies the stored plan, runs independent verification, scans candidate bytes, promotes only clean digest-matched candidates into tenant-private immutable storage, and atomically persists derivative lineage, manifest, current version, and audit event.
- `VERIFIED` derivatives alone can be downloaded. `INCOMPLETE` derivatives retain their label and are withheld from download. Failed policy, verifier, or scanner paths return `BLOCKED` and produce no downloadable artifact.
- Original retrieval now always resolves the original version kind and does not follow the current-version pointer.
- Browser edit tab exposes category authorization, draft/patch/preflight/reviewer/release flow, verification checks, version history/download labels and audit history. Existing data corrections remain separate from native document changes.
- `scripts/e2e_csv_release_demo.py` drives the real API with separately issued local identities and records evidence at each successful stage.
- Added a guarded host validation entrypoint plus Playwright scenarios for CSV, JSON, XLSX and formula-dependent incomplete release. The browser tests are syntax-checked but not executed; see `HOST_INTEGRATION_VALIDATION.md`.

## Not established by this report

- No live derivative has been downloaded end to end in the current environment. PostgreSQL integration tests are skipped; ClamAV, host parser confinement, and browser E2E have not been exercised together.
- XLSX uses the accepted M0 adapter through the release workflow, but database-backed XLSX release and formula-dependent incomplete handling remain pending live integration proof.
- Redis/Valkey queue and processing event stream are not implemented. Upload and inspection are synchronous.
- There is no server-side sign-in UI; local development uses short-lived server-minted signed tokens and production requires configured OIDC.
- Templates/rules/summaries, PDF/DOCX write-back, 50-document acceptance corpus and optional intelligence providers remain incomplete.

See [END_TO_END_TEST_REPORT.md](END_TO_END_TEST_REPORT.md), [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) and [LOCAL_RUN_GUIDE.md](LOCAL_RUN_GUIDE.md) for evidence and commands.
# Current execution update — 2026-11-08

This report's earlier implementation inventory remains historical. The latest regression is 146 passed, 20 skipped (PostgreSQL), 0 failed. The PostgreSQL role provisioner was corrected to use hidden psql password prompts and to support a non-superuser bootstrap path using temporary SET membership; this has source-level tests only. `scripts/dev/start.sh`, `stop.sh`, and `status.sh` were added. The start command was actually invoked and failed closed on missing `IDW_DATABASE_URL`, so this execution did not start the app. ClamAV/Redis/Playwright/Chromium and real PostgreSQL integration remain unavailable/unverified; M4–M7 remain incomplete. See `FINAL_PRODUCT_ACCEPTANCE.md` for the current decision.
