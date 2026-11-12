# Manager UI progress

Prototype used: docs/design/ScanIn_Channel360_Original_Alignment_Fixed.html (no manager-prototype.html exists; confirmed with the user).
Environment note: clamd was not running (stale socket); started /opt/homebrew/sbin/clamd in the background.

- M1 done: Prototype CSS and layout shell (commit 907f443)
- Push blocked: no GitHub credentials in this environment (git push fails with 'Invalid username or token'; gh not logged in). Commits are local on feat/manager-ui; run `git push -u origin feat/manager-ui` after `gh auth login`.
- M2 done: Workspace (Original, Chunks, map, pulse), /chunks and /preview endpoints (commit ed3959f)
- M3 done: POST /versions/{v}/save one-call flow, same-format text writer, demo v2/v3 (commit a40efe0)
- M4 done: Edits tab per format (text, CSV/XLSX grid, DOCX, PDF forms, read-only states); 18 Playwright flows pass (commit dc06416)
- M5 done: classification, drafts, patches, preflight, release, sidecars, capability panels, audit, AI, identity and paging controls are absent from the UI (grep-verified); backend routes/tables untouched; owner uploads authorised server-side in the save flow (commit 9fc7765)
- M6 done: Playwright 18/18, backend 559 passed/32 skipped, side-by-side screenshots + DIFFERENCES.md, README status (commit 5723767)

## Round 2
- 1 done: edit any version (save from vK creates n+1 parented to vK, becomes latest; row lock serialises concurrent saves; empty-state title is the specific reason)
- 2 done: rendered MD/HTML editor, markdown-it-py + markdownify + nh3, untouched-block rendered-text check rejects mismatches
- 3 done: Playwright runs in its own tenant per run (scripts/host/issue_test_token.py); zz-demo-workspace-clean test asserts the demo workspace gains nothing
- 4 done: text writer returns/records SAVED; folder payload carries outcome
- Verified: backend 565 passed / 32 skipped; Playwright 22/22; push still blocked (no credentials)

## Round 3 — real stakeholder corpus (private fixtures in ignored `Policy Copies/`; nothing from them is committed)
- M0 done: `/Policy Copies/` and `/.private/` gitignored (check-ignore verified, nothing was tracked); inventory verified: 30 occurrences / 17 unique (7 PDF, 9 XLSX, 1 DOCX), 132 pages, 43 formulas, 305 merged ranges; `scripts/corpus/inventory.py`
- M1 done: `scripts/corpus/probe.py` drives every file through the real API in a fresh tenant. Found: all 7 PDFs `edit=none`; 2 large PDFs failed import (inspect output exceeded 2 MB sandbox cap because of per-character spans — removed, unused); chunks are arbitrary fragments; signature state not classified
- M2 done: `packages/core/pdf_text.py` genuine content-stream text editor (original glyphs removed, original font reused, fit check); measured vs PyMuPDF; Apryse/Nutrient blocked on trial keys; decision in `docs/pdf-editing-engine.md`; `tests/unit/test_pdf_text.py` 5 pass
- M3 done (c6dc371 + this commit): sandboxed edit worker, `/pdf-text` + `/pdf-edit/preview` + save (`pdf_edits`), signature classification (signed / range mismatch stay read-only with specific reasons), disclosed substitute font, copy-on-write forms, ActualText fix, render-ink + per-line text checks; UI `PdfTextEditor`; backend 588 pass, Playwright 27 pass (second isolated stack :8010/:5180)
- M4 done: sandboxed XLSX layout extractor (`packages/core/xlsx_layout.py`) + `SheetGrid` (merged spans, widths, heights, styles, picture) for Original and Edits; all 9 real workbooks verified in browser and by package diff; DOCX preserved table/images/links/tracking setting, Track Changes note on save; backend 591 pass, Playwright 37 pass
- M5 in progress: ZIP import
