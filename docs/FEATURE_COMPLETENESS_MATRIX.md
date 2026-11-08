# Feature Completeness Matrix

Updated 2026-11-09. “Validated” below refers to the local synthetic development environment, not production acceptance.

| Capability | Implementation | Evidence and boundary |
| --- | --- | --- |
| M0 XLSX integrity/editor | Implemented | Included in 200 passing Python regression tests; input fixture manifest 2/2; formula-dependent case remains incomplete |
| Tenant isolation and PostgreSQL RLS | Implemented | 22 live PostgreSQL integration tests passed against `channel360_test` |
| Malware scanning | Implemented, fail-closed | Four real ClamAV checks passed against the configured daemon |
| CSV and JSON editing | Implemented | Browser upload-to-verified-download workflows passed; originals remain unchanged |
| XLSX native workflow | Implemented for supported targets | Browser verified release passed; formula-dependent workflow reports `INCOMPLETE` and blocks download |
| Data correction sidecars | Implemented | Exercised in CSV, JSON and XLSX browser workflows |
| Independent approval and rejection | Implemented | Distinct reviewer workflows passed; self-review and cross-tenant checks covered by live integration suite |
| Version history, audit and verified download | Implemented | Browser round trips and database authorization tests passed |
| Local demo sign-in | Implemented for local development | API-issued short-lived roles; not a production identity provider |
| Structural and AI document summaries | Implemented for CSV, JSON, XLSX, PDF and DOCX | No AI is the default deterministic summary. Optional OpenAI-compatible explanations require an explicit request, configured server-side credentials, exact tenant-authorized version, bounded restricted-parser evidence and citations validated against server references. Standard provider tests are mocked; no live provider key/request was used in validation. AI output never affects edit/release permissions |
| PDF preview and AcroForm discovery | Implemented for digital and scanned synthetic PDFs | PDF.js Chromium canvas/page navigation and fields pass; scanned pages render without OCR; encrypted is rejected and XFA is read-only |
| PDF native editing and independent verification | Synthetic acceptance subset passed | 50-case corpus: 40 editable, 8 read-only and 2 rejected. Browser verified downloads cover text, checkbox, radio and dropdown; qpdf/PDFium, prefix, appearance and source-preservation checks pass. Commercial producer diversity remains unverified |
| DOCX preview | Partial | Sandboxed OOXML text/table/header/footer inspection displays in browser. LibreOffice page preview cannot run in this host; do not represent extracted text as a rendered preview |
| DOCX constrained editor and verifier | Core implementation present; runtime workflow gated | Single-run adapter, provenance, package checks and unsupported structures have unit checks. UI disables native changes and API preflight stays draft until isolated LibreOffice visual/reflow verification is available. No verified DOCX derivative has been released |
| Offline DOCX candidate diagnostics | Implemented, diagnostic-only | Hashes, native target fingerprints/values, package change inventory, canonical XML, style/media, provenance and optional PDF render comparison. Exact supplied pair passes structural checks; its local rendered check remains `NOT_VERIFIED`. Never authorizes a release |
| Durable work queue, retries and events | Not implemented | Synchronous ingestion only |
| 50-document PDF acceptance corpus | Passed for current synthetic corpus | 50 expected outcomes recorded; no Adobe/Microsoft-authored form compatibility claim |
| M4 deterministic templates and rules | Planned | Not implemented |
| M5 AcroForm PDF editing | Partial implementation | Narrow end-to-end PDF path validated; milestone acceptance remains open |
| M6 constrained DOCX editing | Partial implementation | Native editing remains blocked until host-isolated LibreOffice render/layout checks pass |
| M7 optional intelligence providers | Planned | Not implemented |

Overall application status: validated development prototype; **not production-ready**. See [known limitations](KNOWN_LIMITATIONS.md).
