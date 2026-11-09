# Live End-to-End Test Report

Updated 2026-11-09. All live data was synthetic, stored in the existing `channel360_test` database and explicitly labeled `Channel360 Browser E2E` tenant. No database or role was created, dropped or reset.

## Results

| Gate | Result |
| --- | --- |
| Python regression | 195 passed, 22 skipped, 0 failed (34 warnings); skips remain service-dependent and do not count as passes |
| PostgreSQL integration/security | 22 passed against `channel360_test` |
| ClamAV | 4/4 passed: clean synthetic accepted; EICAR quarantined; executable rejected; scanner unavailable fail-closed |
| Playwright Chromium | 13/13 passed |
| React TypeScript/Vite production build | Passed; PDF.js is lazy-loaded so the initial JS is about 277 KB (83 KB gzip) |
| M0 source fixture hashes | 2/2 passed; full M0 tests are in Python regression |
| Parser loopback boundary probe | Passed on this host; sampled RSS limitation remains |
| Frontend | `http://127.0.0.1:5174` returned HTTP 200 |
| Backend | `http://127.0.0.1:8000/health/ready` returned ready; `/docs` returned HTTP 200 |

## Browser coverage

- CSV, JSON, XLSX and supported unsigned AcroForm PDF uploaded through the live UI and completed the persisted workflow through reviewer approval, independent verification and browser download.
- CSV/JSON/XLSX/PDF browser downloads were compared byte-for-byte with API-authorized derived bytes. Source digests/bytes, version lineage, audit history and unrelated values were checked. PDF additionally passed qpdf, appearance checks, exact source-prefix preservation and PDFium pixel comparison outside widget areas.
- PDF.js canvas pixels were asserted nonblank for digital, XFA read-only and scanned image-only PDFs. The 2-page form exercised next/previous navigation. Encrypted PDFs were rejected; signed/certified/XFA targets remain read-only in unit coverage.
- DOCX body/table/header/footer extraction appears in the browser. Native DOCX edit controls are disabled and API preflight rejects with `critical_docx_verification_unavailable` while leaving the plan in `draft`; no reviewer queue item, derivative version or download is created. Tracked-change DOCX is read-only in-browser.
- Separate reviewer rejection and formula-dependent XLSX `INCOMPLETE` cases passed; incomplete derivative download returned HTTP 409.
- PDF browser corrections covered text, checkbox, radio-group and dropdown fields. The qpdf-rewritten form workflow additionally exercises a distinct xref/object-stream structure.
- The synthetic PDF corpus runner completed 50/50 cases (40 editable, 8 read-only, 2 rejected), recording expected and actual outcomes under ignored `.runtime/pdf-acceptance-corpus/`. It exercised ReportLab forms, Pillow-created scanned pages, pypdf restriction fixtures and qpdf-rewritten ReportLab forms; external commercial producer diversity remains untested.
- All workflows used the isolated E2E tenant and unique generated names. Existing records remain untouched.

## Screenshots

Credential-free synthetic screenshots are under [`artifacts/ui-redesign/`](../artifacts/ui-redesign/):

- `dashboard.png`, `library.png`
- `native-correction-desktop.png`, `native-correction-mobile.png` (1440×1000 and 390×844 viewports)
- `approval-queue-desktop.png`, `approval-queue-mobile.png`
- `docx-workspace-desktop.png`, `docx-workspace-mobile.png`
- `docx-blocked-verification-desktop.png`, `docx-blocked-verification-mobile.png`
- `xfa-pdf-read-only.png`, `scanned-pdf-read-only.png`, `encrypted-pdf-read-only.png`, `tracked-docx-read-only.png`
- `verified-release-desktop.png`, `verified-release-mobile.png`, `reviewer-approved-desktop.png`, `reviewer-rejection-outcome-desktop.png`, `incomplete-verification-desktop.png`

Synthetic verified downloads, JUnit files, Playwright traces/videos, local tokens, runtime state and service logs remain ignored by Git.

## Acceptance boundary

**PDF preview, native edit and independent verification passed for the tested synthetic unsigned AcroForm subset.** The 50-case corpus passes. Compatibility with Adobe/Microsoft-authored and other external-producer forms remains unverified.

**DOCX rendered preview, native editing and actual visual/reflow verification are NOT_VERIFIED.** The ordinary isolated invocation fails with `sandbox_apply: Operation not permitted`; diagnostic runs under the applied Seatbelt profile consistently crash during LO startup at `macxp_resolveAlias → osl::realpath → osl_setCommandArgs`. A harmless probe confirms the profile denies outside-tree reads and loopback networking. No isolated office worker runtime is installed. LO is not run unsandboxed, and DOCX remains fail-closed before approval. The PDFium glyph-geometry comparator and synthetic adversarial tests are implemented, but cannot substitute for actual DOCX renders.

See [M5/M6 implementation report](M5_M6_IMPLEMENTATION_REPORT.md), [known limitations](KNOWN_LIMITATIONS.md), and [security validation](SECURITY_VALIDATION_REPORT.md). M4 templates and M7 OCR/AI remain deferred.
