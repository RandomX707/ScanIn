# Final Product Acceptance — Development Scope

Updated 2026-11-08.

## Decision

The local development workflows and integration evidence below passed for their tested synthetic scopes. M5 PDF is a narrow, usable implementation with open corpus/gate work. M6 DOCX is deliberately blocked at preflight because required isolated rendering is unavailable. **Neither M5 nor M6 is accepted as a complete milestone, and the product is not accepted for production use.**

## Current evidence

- Python regression: 171 passed, 22 skipped, 0 failed (34 warnings). Skipped service-dependent tests are not counted as passes.
- PostgreSQL integration/security suite: 22 passed against the existing `channel360_test` database.
- ClamAV: 4/4 real daemon checks passed.
- Browser suite: 9/9 Chromium tests passed, including CSV/JSON/XLSX/PDF verified releases, DOCX fail-closed preflight, encrypted/XFA/scanned PDF and tracked-change DOCX read-only cases, reviewer rejection, and formula-dependent incomplete release.
- React TypeScript/Vite production build: passed.
- M0 input workbook hashes: 2/2 passed; full M0 behavior is included in Python regression.
- Parser loopback isolation probe passed on this host. Darwin parser RSS is parent-sampled every ~50 ms; a hard `RLIMIT_AS` memory cap is unavailable here.
- Live URLs verified during validation: `http://127.0.0.1:5174` and `http://127.0.0.1:8000/docs`.

## Milestone readiness

| Milestone | Readiness |
| --- | --- |
| M0 | Accepted standalone XLSX engine scope; formula-dependent outputs remain `INCOMPLETE` |
| M1 | Core tenant, registry, storage, inspection and scanning paths validated. Partial overall: queue/events, full corpus and independently verified OS parser network isolation remain open |
| M2 | CSV/JSON/XLSX correction, separate review, independent verification, immutable original and download workflow validated locally |
| M3 | XLSX is integrated into persisted approval/release workflow; incomplete formula dependencies remain nondownloadable |
| M4 | Deferred; templates/rules not implemented |
| M5 | Partial. Synthetic unsigned AcroForm PDF completed the browser approval, qpdf/PDFium verification, exact-prefix preservation and authorized download path. Full PDF-1..PDF-5 corpus remains incomplete. |
| M6 | Partial and host-blocked. OOXML adapter and verifier are unit exercised, but no DOCX can enter approval because isolated LibreOffice visual/reflow verification returns `NOT_VERIFIED`. |
| M7 | Deferred; optional OCR/AI integrations are not implemented |

DOCX preview is safely extracted text only on this host. It is not represented as a rendered Word preview. The branch must not be described as completing full M5/M6 milestone acceptance until the multi-producer corpus is built and DOCX critical rendering/reflow checks pass on the configured sandbox host.

See [README](../README.md), [known limitations](KNOWN_LIMITATIONS.md), [security validation](SECURITY_VALIDATION_REPORT.md), and [live E2E report](LIVE_END_TO_END_TEST_REPORT.md).
