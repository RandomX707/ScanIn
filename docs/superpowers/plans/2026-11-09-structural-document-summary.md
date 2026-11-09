# Structural Document Summary Implementation Plan

1. Add failing unit tests for deterministic summaries across CSV, JSON, XLSX, PDF and DOCX, plus unknown/malformed/scanned cases and contract limits.
2. Implement the provider interface and bounded structural summary builder inside `parse_entry.inspect_bytes`; carry the summary through `ParserReport` and bind IDs in the tenant-scoped view response.
3. Add API-level tests for source/version binding, tenant isolation, and no changes to correction/release state.
4. Add the Summary tab to the existing workspace; preserve the viewer, make Summary default after inspection, and clear state on document changes.
5. Add browser assertions/screenshots for CSV, XLSX, PDF and DOCX, including document switching.
6. Run frontend build, Python/PostgreSQL/ClamAV/Playwright/PDF/M0 regressions; inspect Git staging, commit, push feature branch, update draft PR without merging.

Do not start Phase 2 AI, M4 templates, M7, or rendering infrastructure.
