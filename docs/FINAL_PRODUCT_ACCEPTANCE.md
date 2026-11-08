# Final Product Acceptance — Development Scope

Updated 2026-11-08.

## Decision

The local synthetic development workflows for M0–M3 are validated to the scope listed below. The whole product is **not accepted for production use**. Authentication, operations, storage, queueing, parser isolation and the planned M4–M7 capabilities still require work.

## Current evidence

- Python regression: 157 passed, 22 skipped, 0 failed (34 warnings). Skipped service-dependent tests are not counted as passes.
- PostgreSQL integration/security suite: 22 passed against the existing `channel360_test` database.
- ClamAV: 4/4 real daemon checks passed.
- Browser suite: 6/6 Chromium tests passed, including CSV, JSON, XLSX, approval, rejection, and formula-dependent incomplete release.
- React TypeScript/Vite production build: passed.
- M0 input workbook hashes: 2/2 passed; full M0 behavior is included in Python regression.
- Live URLs verified during validation: `http://127.0.0.1:5174` and `http://127.0.0.1:8000/docs`.

## Milestone readiness

| Milestone | Readiness |
| --- | --- |
| M0 | Accepted standalone XLSX engine scope; formula-dependent outputs remain `INCOMPLETE` |
| M1 | Core tenant, registry, storage, inspection and scanning paths validated. Partial overall: queue/events, full corpus and independently verified OS parser network isolation remain open |
| M2 | CSV/JSON/XLSX correction, separate review, independent verification, immutable original and download workflow validated locally |
| M3 | XLSX is integrated into persisted approval/release workflow; incomplete formula dependencies remain nondownloadable |
| M4–M7 | Planned, not implemented in this pass |

See [README](../README.md), [known limitations](KNOWN_LIMITATIONS.md), [security validation](SECURITY_VALIDATION_REPORT.md), and [live E2E report](LIVE_END_TO_END_TEST_REPORT.md).
