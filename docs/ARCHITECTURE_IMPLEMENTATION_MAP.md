# Architecture Implementation Map

Updated 2026-11-08. Validated on local synthetic data; not a production readiness claim.

| Area | Implementation | Evidence / boundary |
| --- | --- | --- |
| M0 XLSX | `packages/core/adapters/xlsx`, planning and independent verifier | Python regression passes; source fixture hashes 2/2; dependent formulas remain incomplete |
| API and local demo auth | `packages/api` | Signed tenant identity; loopback local-only session issuer; production auth integration not implemented |
| PostgreSQL/RLS/audit | `packages/db`, `packages/audit`, `packages/registry` | 22 live PostgreSQL tests pass on `channel360_test` |
| Ingestion and storage | `packages/ingestion`, `packages/storage` | ClamAV 4/4 live checks pass; local filesystem backend only |
| Worker boundary | `packages/workers`, worker roles/grants | Restricted parser loopback probe passes on current macOS host. Darwin memory control is parent-sampled RSS (~50 ms overshoot), not kernel-enforced `RLIMIT_AS`; host OS boundary still requires platform-specific review |
| Native adapters | CSV, JSON, supported XLSX and narrow PDF AcroForm | Browser verified-release workflows pass for CSV/JSON/XLSX/PDF; PDF release checks qpdf and PDFium visual preservation. Incomplete/blocked results are nondownloadable |
| DOCX adapter and verifier | `packages/core/adapters/docx`, `packages/core/verification/docx.py` | Constrained OOXML edits, provenance and structural checks are implemented. LibreOffice critical visual/reflow check cannot run in this host, so API preflight and UI remain read-only |
| Viewer and UI | API version view and `apps/web` | CSV/JSON/XLSX and PDF.js canvases; DOCX extracted-text inspection; local demo sign-in, library, correction and review views; 13 Playwright tests pass |
| Corrections and release | `packages/workflow`, `packages/api/documents.py`, registry/storage | Sidecar corrections, frozen plans, reviewer decisions, verification, audit and verified-only download exercised |
| Durable jobs/events | None | Not implemented; ingestion is synchronous |
| M4 templates and rules | None | Planned, not implemented |
| M5 PDF native editing | `packages/core/adapters/pdf`, `packages/core/verification/pdf.py` | Synthetic subset: 50 corpus cases and text/checkbox/radio/dropdown browser releases pass; external producer diversity remains unverified |
| M6 DOCX native editing | `packages/core/adapters/docx`, `packages/core/verification/docx.py`, `packages/workers/office.py` | Partial: package writer and verifier are exercised at unit level; host isolation blocks LibreOffice, so native editing is gated and DOCX-1..DOCX-5 are NOT_VERIFIED as a milestone |
| M7 optional intelligence | None | Planned; deterministic core has no AI dependency |
