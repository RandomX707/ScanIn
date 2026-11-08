# Known Limitations

Channel360 is a validated local prototype, not production-ready software.

## Security and operations

- Local demo identities are development-only. There is no production identity provider, account lifecycle, MFA, enterprise SSO or production key management.
- Ingestion and inspection are synchronous. Durable Redis/Valkey work queues, retry policy, job recovery and an authenticated event stream are not implemented.
- PostgreSQL 16 RLS and approval enforcement were exercised by 22 live integration tests against the dedicated `channel360_test` database. This does not replace a production threat model, operational monitoring or deployment security review.
- ClamAV clean, EICAR, rejected-content and scanner-unavailable cases passed against the local daemon. Production scanner capacity, signature update operations and failover remain operator concerns.
- The parser has bounded-process controls and tests, but OS-level network denial has not been independently validated on every target host.
- Development filesystem storage is not a production object-storage, backup or retention solution.

## Product scope

- CSV, JSON and supported XLSX targets can be inspected and corrected. Unsupported structures and formula-dependent XLSX results can remain `INCOMPLETE`; those derivatives cannot be downloaded.
- PDF.js renders digital and scanned PDFs. Supported unsigned AcroForm text, checkbox, radio and choice edits use the persisted review/release workflow and critical qpdf/PDFium verification. The tested synthetic PDF path is usable, but the full M5 acceptance gate remains open: not all PDF-1..PDF-5 variants and no 50-document multi-producer corpus are complete.
- DOCX OOXML inspection and single-run editing/provenance code exist. In this host `sandbox-exec` refuses the isolated LibreOffice renderer. DOCX native controls are disabled and API preflight refuses to freeze/submit; no derivative can reach reviewer approval or download. DOCX page preview and visual/reflow checks remain NOT_VERIFIED.
- The acceptance corpus is not yet the planned 50-document corpus.
- M4 deterministic templates/rules/summaries and M7 optional intelligence providers remain planned, not implemented.
- The M0 image object has a 1×1 worksheet anchor, so recognizable on-sheet appearance has not been verified even though its workbook preservation tests pass.

## Current local evidence — 2026-11-08

- Python regression: 171 passed, 22 skipped, 0 failed. The skips are service-dependent and are not counted as passes.
- PostgreSQL integration/security: 22 passed against `channel360_test`.
- ClamAV: 4/4 passed (clean, EICAR quarantine, rejected executable, scanner unavailable fail-closed).
- Playwright Chromium: 9/9 passed (CSV, JSON, XLSX and PDF verified-release downloads; DOCX read-only/preflight guard; encrypted/XFA/scanned PDF and tracked-change DOCX behavior; login/navigation; reviewer rejection; formula-dependent `INCOMPLETE` with download denied).
- React TypeScript/Vite production build: passed.
- M0 workbook source fixture hashes: 2/2 passed; full M0 acceptance tests are included in Python regression.
- Parser loopback probe passed on the current host. Darwin memory enforcement remains sampled parent-side RSS monitoring (up to about 50 ms overshoot); it is not a kernel `RLIMIT_AS` cap.
- DOCX visual verification returns critical `NOT_VERIFIED`; the workflow remains blocked and native controls are disabled.

See [README](../README.md) for milestone status and setup.
