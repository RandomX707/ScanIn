# Feature Completeness Matrix

Updated 2026-11-09. “Validated” below refers to the local synthetic development environment, not production acceptance.

| Capability | Implementation | Evidence and boundary |
| --- | --- | --- |
| M0 XLSX integrity/editor | Implemented | Included in 228 passing Python regression tests; input fixture hashes 2/2; formula-dependent case remains incomplete |
| Tenant isolation and PostgreSQL RLS | Implemented | 23 live PostgreSQL integration tests passed against `channel360_test` |
| Malware scanning | Implemented, fail-closed | Four real ClamAV checks passed against the configured daemon |
| CSV and JSON editing | Implemented | Browser upload-to-verified-download workflows passed; originals remain unchanged |
| XLSX native workflow | Implemented for supported targets | Browser verified release passed; formula-dependent workflow reports `INCOMPLETE` and blocks download |
| Data correction sidecars | Implemented | Exercised in CSV, JSON and XLSX browser workflows |
| Independent approval and rejection | Implemented | Distinct reviewer workflows passed; self-review and cross-tenant checks covered by live integration suite |
| Version history, audit and verified download | Implemented | Browser round trips and database authorization tests passed |
| Local demo sign-in | Implemented for local development | API-issued short-lived roles; not a production identity provider |
| Structural and AI document summaries | Implemented for CSV, JSON, XLSX, PDF and DOCX | No AI is the default deterministic summary. Optional OpenAI-compatible explanations require an explicit request, configured server-side credentials, exact tenant-authorized version and bounded restricted-parser evidence. Evidence IDs are validated against server references; text-overlap screening is heuristic, not factual verification. Standard provider tests are mocked; no live provider key/request was used in validation. AI output never affects edit/release permissions |
| PDF preview and AcroForm discovery | Implemented for digital and scanned synthetic PDFs | PDF.js Chromium canvas/page navigation and fields pass; scanned pages render without OCR; encrypted is rejected and XFA is read-only |
| PDF native editing and independent verification | Synthetic acceptance subset passed | 50-case corpus: 40 editable, 8 read-only and 2 rejected. Browser verified downloads cover text, checkbox, radio and dropdown; qpdf/PDFium, prefix, appearance and source-preservation checks pass. Commercial producer diversity remains unverified |
| DOCX preview | Implemented as an extracted-text preview | Restricted OOXML extraction displays paragraphs, tables, headers and footers; it is explicitly not a rendered-page preview |
| DOCX constrained editor and structural verifier | Implemented under the default structural profile | Supported single-run edits pass target intent, source binding, package/XML validity, provenance, permitted-part, content and preservation checks; approval and ClamAV remain required. Result is `STRUCTURALLY_VERIFIED`; visual checks are `NOT_PERFORMED` and are disclosed in UI/API/manifests |
| DOCX strict visual profile | Available, host-limited | `structural_and_visual` adds isolated LibreOffice/PDFium rendering checks and blocks if unavailable. This host's sandboxed renderer remains unavailable; this does not block structural-profile releases |
| Offline DOCX candidate diagnostics | Implemented, diagnostic-only | Hashes, native target fingerprints/values, package change inventory, canonical XML, style/media, provenance and optional PDF render comparison. Exact supplied pair passes structural checks; its local rendered check remains `NOT_VERIFIED`. Never authorizes a release |
| Durable work queue, retries and events | Not implemented | Synchronous ingestion only |
| 50-document PDF acceptance corpus | Passed for current synthetic corpus | 50 expected outcomes recorded; no Adobe/Microsoft-authored form compatibility claim |
| M4 deterministic templates and rules | Planned | Not implemented |
| M5 AcroForm PDF editing | Partial implementation | Narrow end-to-end PDF path validated; milestone acceptance remains open |
| M6 constrained DOCX editing | Structural release implemented; strict visual profile NOT_VERIFIED | Standard structural edit-to-download workflow does not launch LibreOffice. No claim is made about identical pagination, line wrapping, fonts or appearance across Word processors. Historical host visual-render limitation remains documented |
| M7 optional intelligence providers | Planned | Not implemented |

Overall application status: validated development prototype; **not production-ready**. See [known limitations](KNOWN_LIMITATIONS.md).
