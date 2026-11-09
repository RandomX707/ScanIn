# Structural Document Summary Design

## Goal

Provide a deterministic, read-only plain-English structural summary for inspected source versions in the existing Channel360 workbench. The summary must be tenant-scoped, tied to the immutable source hash/version, and must not affect categories, edit plans, approvals, verification, or release authorization.

## Approach

Generate the summary in the existing restricted parser process from the same original bytes already used for inspection. Reuse format adapters and existing bounded PDF/DOCX extraction details where available. Attach the contract to the existing version-scoped `view` response after the tenant-scoped repository lookup. The React workspace renders it in a new Summary tab while retaining the source viewer.

This avoids untrusted parsing in the API/UI, a second endpoint and competing parser run, and target-array truncation affecting statistics. It also ensures a summary follows the version hash rather than a mutable document-level cache.

## Contract and providers

The structural provider returns `source_document_id`, `source_version_id`, `source_sha256`, `format`, optional `apparent_document_type`, a 40–100 word maximum description, statistics, observed fields/sections, limitations, `generation_method: structural`, and evidence references. It avoids raw field values and makes no permissions claims. Unknown/malformed content returns a bounded honest unsupported/unavailable result or the existing parser rejection.

A small provider protocol allows later `StructuralSummaryProvider` and future `AIExplanationProvider` implementations. No AI control is exposed until the latter exists. Phase 2 must be explicit, evidence-grounded, labeled, and incapable of changing authorization or verification outcomes.

## Format rules

CSV uses parsed full rows/headers and conservative insurance field-name recognition. JSON reports root shape, bounded key/nesting descriptions and evidence pointers. XLSX reports sheet names and reliable dimensions/header rows from workbook cells. PDF reports page count, readable-text/scanned status and AcroForm field metadata. DOCX reports safely extracted headings, paragraphs, tables, headers and footers; it does not require office rendering. Type labels require supporting structural evidence; otherwise purpose is described as unclear.

## Security and failure behavior

Summaries are generated inside the existing restricted parser boundary from a source whose SHA is verified by the parent. The API binds document/version IDs only after tenant-scoped authorization. No summary content is logged or persisted. Existing unsupported, malformed, or parser-failure behavior remains fail-closed. Summary output is presentation-only.

## Validation

Unit tests cover all formats, unknown/malformed data, scanned PDF, determinism and safe evidence. API tests cover version binding, tenant isolation and unchanged authorization behavior. Browser tests cover tab order/default, real summaries and switching without stale content. Existing regression suites remain required.
