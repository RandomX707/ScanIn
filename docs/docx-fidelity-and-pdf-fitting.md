# High-fidelity DOCX editing and smart PDF text fitting

Base: `main` @ `af02ec5` (the deployed release). Scope: the Edits experience for Word documents and longer replacement text in ordinary
(unsigned) PDFs. Signed-PDF handling, Excel, ZIP, authentication, version lineage, audit chain and download gates are unchanged.

## 1. DOCX: the Edits tab shows the real pages

**Problem.** Original rendered the document with LibreOffice (colours, table geometry, images). Edits rebuilt a simplified HTML view, so the
same file looked different in each tab.

**Design.**

```
Edits tab  ──►  PageViewer (same LibreOffice pages as Original, GET …/render, cached by version+sha)
            ──►  edit panel + synchronized list of native text targets (from the existing sandboxed inspection, /preview → blocks)
            ──►  Preview changes: POST …/docx-edit/preview  → candidate DOCX in memory → sandboxed LibreOffice → PDF (never stored)
            ──►  Save as next version: the UNCHANGED verified native save path (/save → edit set → verification → release)
```

* **Editing surface = the faithful page rendering.** No HTML is ever saved as DOCX and no PDF→DOCX conversion is used anywhere.
* **Native targets.** Each editable text is a single-run `w:t` identified by `docx:<part>:p<i>:r<j>:t<k>` plus a SHA-256 fingerprint of that
  node. Identical texts in different cells have different ids, so a selection can never edit "the other Yes".
* **Where is it on the page? Only when verified** (`apps/web/src/lib/docxLocate.ts`). The rendering carries no native ids, so a block is placed
  on a page only if its text occurs exactly once in the rendered text *and* exactly one block has that text. Repeated labels, identical cells,
  repeating header/footer text and text split across pages stay **unlocated** and are reached from the list (marked `list`). Nothing is guessed.
  On the Health Quote document 33 of 40 editable texts on the sampled view are verified; the rest use the list.
* **Draft preview** (`packages/api/render.py: docx_edit_preview`, `DocxAdapter.draft`): requires the owner-edit permission, applies the edits to
  a copy in memory after re-validating every id, editable flag and fingerprint against freshly computed targets, re-reads the candidate and
  asserts that it differs from the source only at the edited targets, renders it in the same sandboxed renderer (shared 2-slot semaphore,
  timeout and size limits), returns the PDF with `Cache-Control: no-store`. No file, version, edit set or audit row is created. Stale
  fingerprint → 409, other tenant → 404, read-only/multi-run target → 422 with a plain message.
* **UX.** "This version" / "Preview of your changes" tabs; a stale draft is labelled *out of date* until re-previewed; if the page count changes
  a reflow warning is shown; per-text *Revert*, *Discard changes*, search, "Editable text only". The Save button, version naming, History and
  Compare are the existing ones.
* **Fallback.** If the page renderer is unavailable the previous text editor is used (`preview.render.available`).

## 2. PDF: layout-aware fitting (`packages/core/pdf_text.py`)

The content-stream interpreter now also records **table borders and rules** (stroked lines, stroked rectangles, thin filled rectangles), and
the **clip** and **text direction** in effect for every run.

`PdfTextEngine.plan_fit` decides, from geometry only (nothing is written):

| Order | Strategy | Limit | Mechanism |
|---|---|---|---|
| A | natural placement | original size/shape | bounded by neighbouring text (2 pt gap), borders (1.5 pt), clip, page margin; alignment inferred from the cell (left/right/centre) when both borders are detected |
| B | `size` | ≥ 85 % of the original size | `Tf` before the new text, restored after |
| C | `scale` | ≥ 85 % horizontal | `Tz`, restored after |
| D | `spacing` | ≤ 0.04 em tighter per character | `Tc`, restored after |
| E | `expand` | only if a cell is detected | uses the free space of the same cell on both sides (TJ pen adjustment) |

Never made longer: rotated/transformed text, text a border crosses, text inside a clip that cannot be measured. Fonts without width data, missing
glyphs (no safe substitute) and multi-line text keep their existing refusals. The font program is never replaced for fitting.

API: `POST …/pdf-edit/fit` (read-only) returns per edit `{fits, options[], refusal, cell_detected, anchor}`; `pdf_edits[].fit` selects a strategy for
`/pdf-edit/preview` and `/save`. A default (no `fit`) edit that overflows is refused with code `overflow` and the available options in `details`.

**Verification (inside the sandboxed worker, nothing weakened).** All previous checks remain. Added: `no_border_or_neighbour_collision`
(independent re-measure of the *output*: no vertical rule crosses the new text and no other text box overlaps it; fails closed if a grown edit
cannot be located) and `border_pixels_untouched` (no new ink on border strips next to the edit; removing old ink is fine). Pixel-confinement and
unedited-text checks use the edit's full extent (including a shifted start).

**UX** (`PdfTextEditor.tsx`): an overflow that has safe options opens a sticky panel listing them with the expected difference; *Preview fitted
result* renders the real edited PDF; a "Fitted … by …" note offers *Revert to original value*; no option → plain refusal with the reason. Any edit of
a numeric value shows a non-blocking notice that dependent totals are **not** recalculated (no consistency is claimed).

## 3. Tests

* `tests/unit/test_docx_draft.py` (draft validity, identical-text targets, stale/unknown/duplicate/read-only refusals, only `document.xml` changes).
* `tests/unit/test_pdf_fit.py` (natural fit, option reporting, fitted save verified, following text keeps its state, very long refused, neighbour,
  right-aligned growth, expand, rotated, clip, unknown strategy, short/slightly longer unchanged).
* `scripts/dev/release_smoke.py`: draft preview stores nothing, 409/404 paths.
* Playwright `docx-edit-fidelity.spec.js` (Original vs Edits composition on the real Health Quote document, click-to-target, list selection, draft,
  out-of-date draft, save, derived DOCX, original hash) and `pdf-smart-fit.spec.js` (real Fire policy: natural fit, option panel, fitted preview,
  revert, refusal, save, original unchanged, comparison identifies the edit). Four older specs were migrated from the removed contentEditable
  DOCX editor to the page editor via `e2e/docx-edit-helpers.js`.

## 4. Known limitations

* DOCX: only single-run text can be edited (existing rule); multi-run paragraphs, hyperlinks, fields, content controls and text boxes stay read-only
  and are labelled. Page locations exist only where the text is unique (see above). A draft is rendered by LibreOffice, which can differ slightly
  from Word (fonts); the original remains unchanged.
* PDF: fitting handles one line of text in a detected cell or against neighbouring text. Cell *backgrounds* (filled rectangles) are not treated as
  borders; dashed/curved borders are not detected; text on curved paths and rotated text are never lengthened; table reflow is not attempted;
  a cell whose neighbouring text object sits directly beside the value is refused (real example: 6 of 30 sampled sum-insured amounts in one
  corpus PDF).
* Signed PDFs: unchanged (still require the explicit unsigned working copy). Linux Bubblewrap and container confinement remain not runtime verified.

## 5. Deployment and rollback

No migration, no new dependency, no configuration. Backend and frontend ship together (the new editor calls `/docx-edit/preview` and `/pdf-edit/fit`).
Rollback = redeploy the previous release (`af02ec5`); data written by the new code is ordinary DOCX/PDF versions and remains valid. The draft endpoint
adds no persistent state.

---

# Final acceptance and independent review

Run on isolated resources only (`channel360_iso_fidelity`, `channel360_iso_fidpytest`, ports 8010/5180, own storage); shared services, `channel360_test`
and the release checkout were not touched. Private evidence (screenshots, derived files, per-document JSON) is under `.private/`; nothing below quotes
stakeholder content.

## A. DOCX on the real Health Quote document

| Check | Result |
|---|---|
| Original v1 after the whole flow | byte-identical (SHA-256 equal to the corpus file) |
| Draft preview created a version | no (history length 1 after the draft; 1 after 8 concurrent drafts) |
| Save: lineage | v2 `derived`, parent v1, classification `standard`; save outcome from the verified native path |
| Targets changed by the save | exactly 1 of 175 (ids identical sets); edited value exact |
| Package parts changed | `word/document.xml` plus the existing provenance parts (`docProps/custom.xml`, `[Content_Types].xml`, `_rels/.rels`); none removed |
| `document.xml` after restoring the one edited text | canonically identical to the original, i.e. formatting, shading, borders, merged cells, column/row dimensions, alignment, image references are unchanged |
| Saved file reopened independently | python-docx opens it (1 table, 81 paragraphs); sandboxed LibreOffice renders it (4 pages) |
| **Draft preview vs independent render of the saved DOCX** | **0 differing pixels on all 4 pages** (same candidate) |
| Original render vs saved render | 0 changed pixels on pages 1, 3, 4; page 2: 2,427 pixels inside a 50 × 7 pt box (the edited cell) |
| Colours | yellow premium-row pixels on page 2 identical (43,970) in original, saved and draft |
| Original tab vs Edits tab (before changes) | both use the same `/render` PDF; page proportions equal and coloured/dark pixel shares within the spec tolerances (Playwright `docx-edit-fidelity`) |
| Repeated text in different cells | the real document has no repeated editable text, so a synthetic table (`Yes`, `Yes`, `Yes`; labelled synthetic) was used: editing the middle cell changed exactly that target; the other two stayed `Yes` |
| Texts without a verified page position | 7 of 40 editable blocks; all 7 selected from the synchronized list showed the correct native text and produced a draft (HTTP 200) without creating a version |

## B. PDF: the Fire policy example

The literal original `25,00,00,000` **does not occur** in the supplied Fire policy. Its page-2 sum-insured amounts are three 12-character
values and one total, each followed by a separate adjacent `.00` text object, in right-aligned cells. The exact replacement string
`25,00,00,000,678` was applied to the first of them (the building sum insured).

| Measurement (pt) | Value |
|---|---|
| Field | page 2, 10 pt Helvetica-family font, right edge 565.53, original width 58.31 |
| Cell borders detected | left 475.66, right 584.88 |
| Neighbour | `.00` object starts at 565.54 (0.01 pt gap) |
| Replacement width | 77.53 |
| Available width (right-anchored, inside borders and 1.5 pt padding) | 88.4 (left limit 477.16) |
| Strategy | A, natural placement, right edge kept (grows leftwards); no size, scale or spacing change (size 100 %, Tz 100 %) |
| Verdict | **accepted**; all 14 worker checks pass including `no_border_or_neighbour_collision` and `border_pixels_untouched` |
| Saved derived PDF | v2, parent v1; original byte-identical; 72 pages reopen in pypdf and pdfium |
| Searchable | the new amount is extractable from page 2; the old amount no longer occurs at that position |
| Pixels | 4,083 changed pixels in x 488.0–565.0 pt, y 420–429 pt only; 0 pixels changed on the two cell borders; 0 changed on the other 71 pages; the change stays right of the left limit and ends before the `.00` object |
| Surrounding content | the two other sum-insured amounts, the total and every other page's text are identical |
| No silent reformatting or recalculation | the amount is stored exactly as typed (`…,678` + the original `.00`); the total is untouched; the UI shows the non-blocking "dependent figures are not recalculated" notice |
| Version comparison | 1 modified, 0 added, 0 removed, scan COMPLETE |

The same edit on the other three sum-insured values and the total of that table gives the same result (natural, verified).
Across the corpus, appending `,678` to the 30 sampled thousand-separated amounts: 24 fit naturally (Fire policy), 6 are refused because another text
object sits directly beside the value (QCR health policy); no relaxation was made to force them.
Fitting strategies exercised on real documents: A (natural, right-anchored) and `size` (through the UI on the Fire policy). `scale`, `spacing` and `expand`
are covered by synthetic unit tests with verification, not by real-document runs.

## C. Regression results (final code)

| Suite | PASS | FAIL | SKIP/BLOCKED |
|---|---|---|---|
| Backend `pytest tests` (PostgreSQL env set) | 777 | 0 | 0 |
| Playwright, production build, all specs except ZIP | 94 | 0 | ZIP spec excluded (out of scope); includes the 4 new acceptance tests |
| Non-ZIP stakeholder acceptance (`acceptance_run --skip-zip`) | 17 | 0 | ZIP stage reported OUT OF SCOPE |
| `compare_flow` / `branch_compare_flow` | 31 / 11 | 0 | |
| `xlsx_fidelity` / `xlsx_index_fidelity` | 91 / 36 | 0 | |
| `xlsx_benchmark` / `xlsx_compare_stress` | 32 / 20 | 0 | |
| `docx_fidelity` / `docx_geometry` | 26 / 9 | 0 | |
| `signed_flow` / `security_flow` (signed PDFs, tenant isolation, downloads) | 82 / 23 | 0 | |
| `pdf_api_flow` / `docx_api_flow` / `office_api_flow` | exit 0 | | |
| `release_smoke.py` (isolated stack) | 32 | 0 | |
| Sandbox runtime (`check-sandbox.py`, `tests/unit/test_sandbox.py`) | READY / passes | 0 | Linux Bubblewrap and containers NOT RUNTIME VERIFIED |

**Test inventory.** Baseline 751 (development branch) vs 777 now. `main` @ `af02ec5` collects 698 in a clean checkout: the 751 included 57 parametrisations over
private CSV fixture packs that exist only under the development worktree's `artifacts/` (`test_csv_compact_parser`, `test_csv_summary_one_pass`,
`test_csv_verifier_compact`: 19 each), minus 2 `test_corpus_runner_hardening` cases and 2 `test_dependency_manifest` cases that the development branch does not contain.
This branch adds 22 tests (5 DOCX draft, 17 PDF fitting). Linking those fixture packs into the worktree (untracked) gives 698 + 57 + 22 = 777, so no test was
dropped or excluded. Two earlier observations are explained, not re-run away: a first pytest run showed 32 skips because the PostgreSQL environment variables were
not exported in that shell (skips are reported `NOT_VERIFIED`; with them set: 0 skips), and `pdf_api_flow`/`docx_api_flow` first failed with `IndexError` because
I called them without their required file argument (harness misuse, not a product failure).

## D. Independent review of the three commits

Fixed during this review (each with a test or an acceptance run that showed it):

| # | Finding | Fix |
|---|---|---|
| R1 | Clip state was not inherited by nested form XObjects, so text inside a parent clip could be grown beyond it | clip passed into nested walks |
| R2 | Rectangular clips drawn as `m/l/h` (not `re`) counted as "unmeasurable", refusing every growth in a real policy (all 1,870 sampled runs of one PDF); surfaced as an `acceptance_run` FAIL (`clipped_text` instead of `overflow`) | an axis-aligned polygon is measured as a box; non-rectangular clips still refuse |
| R3 | The collision check ignored other edited texts, so two edits on one line could touch undetected | new `edited_texts_do_not_overlap` verification check (engine ordering already prevented it; this is defence in depth) |
| R4 | The page locator assumed an upright page with origin (0,0), so a rotated/offset rendering could place a highlight wrongly | positions are claimed only for upright, origin-aligned pages; otherwise list only |

Reviewed and found sound: target id + fingerprint validation is recomputed from the stored source for every draft; draft authorisation = tenant RLS + owner-edit permission, 409 stale, 404 other tenant; the
draft candidate is re-read and must differ from the source only at the edited targets; the renderer copies input into a private scratch directory removed in `finally`
(8 concurrent drafts: all 200, 0 leftover scratch directories, no version created); the sandbox and its self-test are the existing ones; draft and save are the same text replacement and
the render of the saved file is pixel-identical to the draft; original files are never written; a refused PDF save raises before anything is staged; comparison of draft-derived versions is the existing comparer.

Open findings, not fixed (documented limitations, no correctness defect found):

* O1. Drafts share the existing 2-slot render semaphore and are not rate-limited or audited per request; heavy draft traffic can delay page previews (`render_busy` after 30 s). The DOCX is parsed in process by the same adapter code the save path already uses.
* O2. `/pdf-edit/preview` and `/pdf-edit/fit` do not require the owner-edit permission (preview never did); the DOCX draft does. Not weaker than before, but inconsistent.
* O3. Alignment inference (left/right/centre) is a geometric heuristic; it can never cause a collision (geometry and output are verified) but can place text differently from what a user expects in unusual cells.
* O4. Fitting works on one text line; filled cell backgrounds, dashed or curved borders and rotated text are not treated; a value directly beside another text object is refused.

## E. Push and deployment readiness

**Recommendation: READY TO PUSH FOR REVIEW.** All suites above pass on the final code with 0 failures and 0 unexplained skips; the four review findings that touched correctness
were fixed and re-verified; the open findings (O1-O4) are limitations, not defects, and are documented.

Deployment: no migration, dependency or configuration change; backend and frontend must be deployed together (the editor calls `/docx-edit/preview` and `/pdf-edit/fit`). Before deploying, rerun
`scripts/dev/release_smoke.py` against the release configuration. Rollback: redeploy `af02ec5`; versions created by this code are ordinary DOCX/PDF files. Not verified: Linux Bubblewrap, containers, ZIP.
