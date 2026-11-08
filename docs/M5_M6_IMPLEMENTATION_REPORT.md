# M5 PDF and M6 DOCX Implementation Report

Updated 2026-11-08. This report follows the M5/M6 scopes and PDF-1..PDF-5 / DOCX-1..DOCX-5 acceptance criteria in [System Design v2.1.1](../Insurance%20Document%20Workbench%20%E2%80%94%20System%20Design%20v2.1.1.md). Synthetic fixtures only. This is not a production acceptance or readiness report.

## Implemented

- PDF.js browser rendering of digital and image-only PDFs, page controls, zoom, AcroForm field discovery, stable field identifiers, editability reasons, and read-only handling for encrypted, signed, certified and XFA documents.
- pypdf incremental writing for supported unsigned AcroForm text, checkbox, radio and choice fields. A small pinned-version compatibility override adds the xref-stream self-entry required by qpdf without rewriting the original byte prefix.
- Independent PDF checks for source binding, frozen-plan digest, qpdf syntax, exact original-byte prefix, field-value readback, target appearance presence, unchanged unrelated fields/page content/annotations, provenance and PDFium pixel equality outside edited widget rectangles.
- Secure OOXML ZIP inspection and single-`w:t`/single-`w:r` DOCX replacement for body, tables, headers and footers. Unsupported structures have target-level reasons. The writer preserves run/paragraph formatting, OOXML part order and compression methods and writes the approved custom-properties provenance parts.
- Independent DOCX package, target, allowed-part, provenance and preservation checks, with critical LibreOffice render/layout verification. A missing or denied render is `NOT_VERIFIED` and `BLOCKED`.
- DOCX UI now labels the source preview as extracted text, presents sidecar correction targets separately, disables native changes, and does not allow a DOCX draft to enter reviewer approval until the critical visual verifier is available. The API independently rejects DOCX preflight before persisting a frozen/submitted plan when rendering is unavailable.
- Browser runner builds synthetic encrypted, XFA, scanned PDF, AcroForm PDF, ordinary DOCX and tracked-change DOCX fixtures under ignored `.runtime` storage.

### Dependency and licence inventory

| Dependency | Pin | Licence information checked locally |
| --- | --- | --- |
| pypdf | 5.4.0 | BSD classifier; upstream license file is included in the installed distribution |
| pypdfium2 | 5.15.1 | Distribution includes Apache-2.0 and BSD-3-Clause texts plus separate PDFium and bundled third-party notices; review those notices before redistribution |
| Pillow | 12.0.0 | MIT-CMU |
| lxml | 5.4.0 | BSD-3-Clause |
| PDF.js (`pdfjs-dist`) | 4.10.38 | Apache-2.0 |
| qpdf | 12.4.2 | Apache-2.0 |
| LibreOffice | 26.8.2.2 | Main distribution includes MPL-2.0 plus other component licences; retain the distribution notices |
| python-docx / ReportLab | 1.2.0 / 4.4.9 | Test-fixture tools only; package metadata reports MIT / BSD |

This is an inventory, not a legal compatibility opinion. PDFium and LibreOffice are distributions with component notices; their complete notices must travel with any redistributed binaries.

## Acceptance evidence

| Gate | Result | Evidence and remaining scope |
| --- | --- | --- |
| PDF preview | **PASS** | Chromium canvas renders two pages, navigates both pages, zooms, and shows synthetic AcroForm targets. A scanned image-only PDF renders without OCR and has no edit targets. |
| PDF native editing | **PASS for tested synthetic subset** | Browser completed text, checkbox, radio and dropdown edits through independent review and release. The checked-in corpus runner covers 50 generated cases from ReportLab, Pillow raster scans, pypdf restrictions and qpdf-rewritten forms. |
| PDF independent verification | **PASS for tested synthetic subset** | qpdf/PDFium checks include original-byte prefix, readback, appearances, pixels outside edited widgets, unchanged source bytes and browser download equality. Corpus runner result: 50/50; 40 editable, 8 read-only, 2 rejected. |
| DOCX preview | **NOT_VERIFIED** | Browser shows safe OOXML-extracted paragraph/table/header/footer text. The required isolated LibreOffice rendered preview cannot execute on this host, so the UI does not represent extracted text as a rendered DOCX page preview. |
| DOCX native editing | **NOT_VERIFIED end to end** | The OOXML writer and provenance checks are unit exercised. Native UI editing is disabled, and live API preflight leaves the edit set in `draft` because the critical render check is unavailable. No DOCX derivative is approved or released. |
| DOCX visual/reflow verification | **NOT_VERIFIED** | The isolated LibreOffice render cannot run. The verifier records critical visual `NOT_VERIFIED`, yields `BLOCKED`, and no derivative download is issued. DOCX-5 wrapping behavior cannot be accepted on this host. |
| Browser acceptance | **PARTIAL — DOCX gate not met** | 13/13 Playwright tests pass. CSV/JSON/XLSX and PDF text/control edits complete verified downloads; DOCX remains read-only/preflight-blocked. |
| Existing regression | **PASS for executed gates** | Python 175 passed/22 skipped; PostgreSQL 22 passed; ClamAV 4/4; frontend build and M0 hashes pass. |

## Design acceptance test status

- **PDF-1:** PASS for a synthetic text field; readback, appearance, prefix and outside-widget pixel checks pass.
- **PDF-2:** PASS in adapter and live browser workflows for checkbox, radio and dropdown values/appearances.
- **PDF-3:** PASS for synthetic signed, certified and XFA read-only inspection; encrypted PDFs are rejected. Browser exercises XFA and encrypted documents.
- **PDF-4:** PASS for synthetic fields; both `MaxLen` and conservative widget-width overflow preflight rejections are tested.
- **PDF-5:** PASS in unit and browser tests; derived bytes start with all original bytes.
- **DOCX-1:** Structural edit coverage for body, table, header and footer passes; required LibreOffice render evidence is NOT_VERIFIED.
- **DOCX-2:** Multi-run, actual hyperlink and field-code targets are non-editable with explicit reasons.
- **DOCX-3:** Protected, macro-bearing and tracked-change packages are read-only; tracked changes also pass browser coverage.
- **DOCX-4:** Provenance-only part inventory and independent structural checks pass at unit level; rendered milestone acceptance remains open.
- **DOCX-5:** NOT_VERIFIED. A real isolated before/after render is required to establish reflow blocking.
- **Corpus:** 50 synthetic cases with expected outcomes and recorded run results pass. Producer diversity is limited to ReportLab, Pillow-generated scan pages, pypdf restriction fixtures and qpdf rewriting ReportLab forms; no Adobe/Microsoft-authored form corpus was tested.

The independent DOCX layout comparator now measures PDFium glyph positions and page assignments, rejects ambiguous repeated targets, wrapped target text, page-count changes and any non-target text movement. Synthetic geometry adversarial tests pass. They do not replace real before/after DOCX renders, which remain unavailable.

## Host limitation and security boundaries

The deny-default Seatbelt profile limits reads to the LibreOffice bundle, system libraries/fonts and the per-job input tree, writes to that job's temporary tree, and denies network access. A harmless probe confirmed an outside-tree marker read and loopback connection both fail with `EPERM`. In the ordinary managed invocation, `sandbox-exec` fails before launch with `sandbox_apply: Operation not permitted`. Host-authorized diagnostic runs got LibreOffice past profile application, but it consistently segfaulted during startup at `macxp_resolveAlias → osl::realpath → osl_setCommandArgs`; changing job CWD, canonicalizing temporary paths, and adding narrowly scoped application-bundle read access did not resolve it. No Docker/Podman/VM or other isolated office worker is installed. LibreOffice was not run unsandboxed. DOCX remains blocked before reviewer submission and release.

The parser loopback isolation probe passes on this host. Darwin does not provide a working hard `RLIMIT_AS` cap here; the process parent samples worker process-group RSS about every 50 ms and kills above 2 GiB, so a small overshoot is possible. Platform-specific OS-level network sandbox review remains necessary. No real customer or patient documents were processed.

## M4 and M7

M4 templates/rules and M7 optional OCR/AI integrations remain deferred. This branch prioritizes M5 and M6; it does not claim either complete milestone acceptance until the corpus and all critical gates pass.
