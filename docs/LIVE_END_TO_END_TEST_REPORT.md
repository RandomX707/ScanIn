# Live End-to-End Test Report

Updated 2026-11-08. All tests used synthetic data in the existing `channel360_test` database and the explicitly labeled `Channel360 Browser E2E` tenant. No database or role was created, dropped or reset during this run.

## Results

| Gate | Result |
| --- | --- |
| Python full regression | 157 passed, 22 skipped, 0 failed; skipped tests are service-dependent |
| PostgreSQL integration and security | 22 passed, 157 deselected |
| ClamAV | 4/4 passed: clean input; EICAR quarantined; executable rejected; scanner unavailable fail-closed |
| Playwright Chromium | 6/6 passed: CSV, JSON, XLSX, local sign-in/navigation/upload, reviewer rejection, formula-dependent `INCOMPLETE` |
| React TypeScript/Vite production build | Passed |
| M0 source fixture hashes | 2/2 passed; all M0 regression tests passed in the Python suite |
| Frontend URL | `http://127.0.0.1:5174` returned HTTP 200 |
| Backend URLs | `http://127.0.0.1:8000/health/ready` returned `ready`; `/docs` reachable |

The CSV, JSON and XLSX Playwright flows exercised upload, scanning, inspection, selected values, separate data correction, native edit plan, review, independent verification, derivative download, unchanged source digest, audit/version history and preservation of unrelated values. The formula-dependent workbook reported `INCOMPLETE`; its derivative download returned HTTP 409 and the UI labels it incomplete with no verified download.

The live database suite covers tenant RLS, cross-tenant denial, reviewer separation, immutable/frozen edit sets, audit chain and approval/release guards. The browser suite also confirmed local role switching clears prior document notifications and that repeated test fixtures use unique names in the isolated E2E tenant.

## Screenshots

Credential-free synthetic screenshots are in [`artifacts/ui-redesign/`](../artifacts/ui-redesign/):

- `showcase-workspace-desktop.png` and `showcase-workspace-mobile.png`
- `showcase-review-queue-desktop.png` and `showcase-review-queue-mobile.png`
- `reviewer-approved-desktop.png` and `reviewer-approved-mobile.png`
- `reviewer-rejection-outcome-desktop.png`
- `verified-release-desktop.png` and `verified-release-mobile.png`
- `incomplete-verification-desktop.png`

The committed screenshot set includes these visual artifacts. JUnit, Playwright traces, videos, local downloads and runtime logs remain ignored.

## Important boundaries

The workbench remains a development prototype. Redis-backed processing, production identity, production storage, cross-platform parser network-isolation evidence, the full 50-document corpus and M4–M7 features are not complete. See [known limitations](KNOWN_LIMITATIONS.md).
