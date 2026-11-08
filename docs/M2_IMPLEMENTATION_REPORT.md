# M2 Corrections and Native Release

Updated 2026-11-08. M2 correction and native release workflows are implemented and exercised locally against PostgreSQL and ClamAV. This does not represent production readiness.

Implemented and validated:

- Append-only tenant-scoped data corrections and native edit sets with typed values.
- Administrator classification, processor-authored plans, frozen preflight and separate reviewer approval/rejection.
- CSV, JSON and supported XLSX application with independent verification.
- Original-file byte preservation, derived version lineage, audit history and verified-only derivative downloads.
- Formula-dependent XLSX remains `INCOMPLETE` and nondownloadable.
- Local browser workflows include cross-tenant isolation, reviewer separation, rejection, approval and real download verification.

Evidence: 22 PostgreSQL integration tests, 4 ClamAV checks and 6 Playwright workflows passed on 2026-11-08. Full evidence and limitations are in [LIVE_END_TO_END_TEST_REPORT.md](LIVE_END_TO_END_TEST_REPORT.md) and [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).
