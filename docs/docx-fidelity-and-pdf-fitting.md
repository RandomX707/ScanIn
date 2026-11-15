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
