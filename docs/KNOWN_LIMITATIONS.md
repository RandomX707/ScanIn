# Known Limitations

- Optional AI explanation uses a process-local five-requests-per-user-per-minute limiter; multiple API worker processes do not share counters. Configure only an approved HTTPS-compatible provider, and use synthetic documents for development. Standard tests mock the provider; no live provider response has been validated. No AI remains available without provider configuration.
- AI evidence identifiers and locations are checked against extracted source evidence. A lexical-overlap heuristic may reject unrelated claims, but it cannot establish that a model statement is factually correct; users must verify important details against the document.

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
- PDF.js renders digital and scanned PDFs. Supported unsigned AcroForm text, checkbox, radio and choice edits use the persisted review/release workflow and critical qpdf/PDFium verification. A 50-case synthetic acceptance run passed, including qpdf-rewritten forms; producer diversity is limited and does not include Adobe/Microsoft-authored forms.
- DOCX OOXML inspection and constrained single-run body/table/header/footer editing use the default structural verification profile. The full release path does not invoke LibreOffice. Its `STRUCTURALLY_VERIFIED` outcome proves the configured package/content checks passed, and explicitly means the visual layout was not checked. It does not guarantee pagination, line wrapping, font rendering or appearance across Word processors.
- The optional `structural_and_visual` DOCX profile still requires the isolated LibreOffice/PDFium renderer. On this host `sandbox-exec` refuses the isolated LibreOffice renderer; this profile fails closed and remains NOT_VERIFIED. It is not silently downgraded.
- DOCX strict-profile geometry-comparison adversarial tests reject wrapping, ambiguous repeated targets, changed page count and non-target glyph movement. They validate the comparator but do not establish that actual Word rendering is unchanged.
- The supplied one-page DOCX pair passes local structural/hash/fingerprint/provenance diagnostics. A separate user-supplied Linux/LibreOffice 25.2.3.2 report says its pixel/layout comparison passed; this host did not reproduce that render. The candidate remains UNVERIFIED and is not an application release.
- The offline CLI `scripts/validate_docx_candidate.py` can compare existing rendered PDF evidence, but does not launch a renderer. On this Mac, installed LibreOffice 26.8.2.2 crashes under the existing sandbox boundary and no OCI/VM worker runtime is installed.
- M4 deterministic templates/rules/summaries and M7 optional intelligence providers remain planned, not implemented.
- The M0 image object has a 1×1 worksheet anchor, so recognizable on-sheet appearance has not been verified even though its workbook preservation tests pass.

## Current local evidence — 2026-11-09

- Python regression: 228 passed, 23 skipped, 0 failed. The skips are service-dependent and are not counted as passes.
- PostgreSQL integration/security: 23 passed against `channel360_test`, including the structural DOCX manifest/download contract.
- ClamAV: 4/4 passed (clean, EICAR quarantine, rejected executable, scanner unavailable fail-closed).
- Playwright Chromium: 15/15 passed, including the DOCX structural edit, independent approval, `STRUCTURALLY_VERIFIED` release, explicit visual-not-performed disclosure, authorized download/reopen, PDF text/checkbox/radio/dropdown downloads, CSV/JSON/XLSX workflows, AI/no-AI/compare interaction, provider failure handling, and formula-dependent `INCOMPLETE` download denial.
- React TypeScript/Vite production build: passed.
- M0 workbook source fixture hashes: 2/2 passed; full M0 acceptance tests are included in Python regression.
- Parser loopback probe passed on the current host. Darwin memory enforcement remains sampled parent-side RSS monitoring (up to about 50 ms overshoot); it is not a kernel `RLIMIT_AS` cap.
- LLM credentials were not configured for this validation, so no external provider request was made. JSON-mode fallback, IP-pinned HTTPS connection behavior, structured parsing and evidence-reference checks were validated with mocks/unit tests only.
- Default DOCX structural release records `STRUCTURALLY_VERIFIED`, `structural_checks=PASS`, and `visual_checks=NOT_PERFORMED`; strict visual release remains blocked when its renderer cannot execute.
- DOCX browser evidence: body text target changed from “Policyholder: Alexander Kumar” to “Policyholder: Alexander Kumar Venkatesh”; original SHA-256 stayed `09f4b1ba62c171d9470fff183b97e00713c0ddc5208fe6960709af169672aaba`; the authorized downloaded candidate SHA-256 is `9fb2fb37d7f11730cdd94cc515f588de14b7dbad8deb7cbe8765cbeb9fcda1c2`. The ZIP reopened successfully and the intended text was present.
- The exact supplied DOCX pair passes the offline structural diagnostic; local visual remains `NOT_VERIFIED`. Eight generated structural surfaces plus repeated targets and negative integrity cases are covered by new tests.
- LibreOffice sandbox diagnostics: ordinary invocation is denied before launch; host-authorized diagnostics show a repeatable startup SIGSEGV in `macxp_resolveAlias → osl::realpath → osl_setCommandArgs` after Seatbelt application. A minimal deny-default probe confirms outside-tree reads and loopback access return `EPERM`. No alternative isolated office runtime is installed.
- PDF synthetic corpus: 50/50 expected outcomes; 40 editable, 8 read-only, 2 rejected. This corpus is not a substitute for broader producer compatibility testing.

## Historical evidence before the structural DOCX profile

Before the explicit structural-only product profile was introduced, DOCX native changes were intentionally read-only because isolated LibreOffice visual verification could not run on this macOS host. At that point the 15/15 Playwright suite validated DOCX fail-closed behavior, and the supplied manual candidate remained diagnostic-only. That prior evidence is retained in the historical screenshots and repository records; it is superseded for the standard DOCX workflow by the current structural-profile results above. The optional strict visual profile continues to use the prior fail-closed boundary.

See [README](../README.md) for milestone status and setup.
