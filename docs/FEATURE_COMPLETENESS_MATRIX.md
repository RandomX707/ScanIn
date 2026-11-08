# Feature Completeness Matrix

Updated 2026-11-08. “Validated” below refers to the local synthetic development environment, not production acceptance.

| Capability | Implementation | Evidence and boundary |
| --- | --- | --- |
| M0 XLSX integrity/editor | Implemented | Included in 157 passing Python regression tests; input fixture manifest 2/2; formula-dependent case remains incomplete |
| Tenant isolation and PostgreSQL RLS | Implemented | 22 live PostgreSQL integration tests passed against `channel360_test` |
| Malware scanning | Implemented, fail-closed | Four real ClamAV checks passed against the configured daemon |
| CSV and JSON editing | Implemented | Browser upload-to-verified-download workflows passed; originals remain unchanged |
| XLSX native workflow | Implemented for supported targets | Browser verified release passed; formula-dependent workflow reports `INCOMPLETE` and blocks download |
| Data correction sidecars | Implemented | Exercised in CSV, JSON and XLSX browser workflows |
| Independent approval and rejection | Implemented | Distinct reviewer workflows passed; self-review and cross-tenant checks covered by live integration suite |
| Version history, audit and verified download | Implemented | Browser round trips and database authorization tests passed |
| Local demo sign-in | Implemented for local development | API-issued short-lived roles; not a production identity provider |
| PDF/DOCX native editing | Not implemented | Read-only or unavailable previews only |
| Durable work queue, retries and events | Not implemented | Synchronous ingestion only |
| 50-document acceptance corpus | Not complete | Only synthetic targeted fixtures are present |
| M4 deterministic templates and rules | Planned | Not implemented |
| M5 AcroForm PDF editing | Planned | Not implemented |
| M6 constrained DOCX editing | Planned | Not implemented |
| M7 optional intelligence providers | Planned | Not implemented |

Overall application status: validated development prototype; **not production-ready**. See [known limitations](KNOWN_LIMITATIONS.md).
