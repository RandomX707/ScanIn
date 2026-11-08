# Architecture Implementation Map

Updated 2026-11-08. Validated on local synthetic data; not a production readiness claim.

| Area | Implementation | Evidence / boundary |
| --- | --- | --- |
| M0 XLSX | `packages/core/adapters/xlsx`, planning and independent verifier | Python regression passes; source fixture hashes 2/2; dependent formulas remain incomplete |
| API and local demo auth | `packages/api` | Signed tenant identity; loopback local-only session issuer; production auth integration not implemented |
| PostgreSQL/RLS/audit | `packages/db`, `packages/audit`, `packages/registry` | 22 live PostgreSQL tests pass on `channel360_test` |
| Ingestion and storage | `packages/ingestion`, `packages/storage` | ClamAV 4/4 live checks pass; local filesystem backend only |
| Worker boundary | `packages/workers`, worker roles/grants | Restricted parser and least-privilege DB role; host OS network denial is not independently verified |
| Native adapters | CSV, JSON and supported XLSX adapters | Browser verified-release workflows pass; incomplete results remain nondownloadable |
| Viewer and UI | API version view and `apps/web` | CSV/JSON/XLSX viewers, local demo sign-in, library, correction and review views; six Playwright tests pass |
| Corrections and release | `packages/workflow`, `packages/api/documents.py`, registry/storage | Sidecar corrections, frozen plans, reviewer decisions, verification, audit and verified-only download exercised |
| Durable jobs/events | None | Not implemented; ingestion is synchronous |
| M4 templates and rules | None | Planned, not implemented |
| M5 PDF native editing | None | Planned; PDF remains read-only |
| M6 DOCX native editing | None | Planned; DOCX remains read-only |
| M7 optional intelligence | None | Planned; deterministic core has no AI dependency |
