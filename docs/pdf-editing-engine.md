# PDF content-editing: technology evaluation (M2)

Status: proof of concept measured on the private stakeholder corpus (13-, 72-, 5-, 24-, 12-, 4- and 2-page policy/QCR PDFs).
No document content left the machine. Numbers below are metrics only.

## Requirements (from the acceptance spec)
Ordinary selectable-text PDFs; real text replaced (never an annotation, white box or overlay); original font kept where
possible; overflow/clipping detected; searchable same-format output; independent re-open + text + pixel verification;
local processing; immutable original.

## Options
| | In-house engine (pypdf + pypdfium2; fontTools later) | PyMuPDF / MuPDF | Apryse SDK | Nutrient (PSPDFKit) |
|---|---|---|---|---|
| Licence | BSD/MIT-style, no fees | AGPL-3.0 **or** paid Artifex commercial licence; pricing not public ([Artifex](https://artifex.com/licensing)) | Commercial only; production key from sales, trial key watermarks output, price unpublished ([Apryse docs](https://docs.apryse.com/documentation/web/faq/add-license)) | Commercial; self-hosted Document Engine is annual, quote-based; offline licence exists ([Nutrient](https://www.nutrient.io/sdk/pricing/)) |
| Local / private | yes | yes | yes (key check aside) | yes with offline licence |
| Fidelity on real files | measured below | measured below | **not evaluated: needs trial key/account** | **not evaluated: needs trial key/account** |
| Deploy cost | none | AGPL obligations for a network service, or licence fee | per-deployment licence | per-deployment licence |

## Measured on the real 13-page CAR-type PDF (same edit: drop the last word of a heading line)
| | In-house | PyMuPDF (redact + insert) |
|---|---|---|
| Original glyphs removed from content stream | yes | yes (redaction) |
| Font of new text | the original embedded subset font | Helvetica (substituted; original Tahoma not reusable) |
| Changed pixels | 2,108, all inside the edited run | 5,260, spreading down the page (y 87→716 pt) |
| New text found by independent parser / old text gone | yes / yes | yes / n/a |
| Other 12 pages | pixel-identical | not measured |
| Output size vs original | larger (needs stream compression, planned) | smaller |

## Coverage of the in-house engine (structural readiness of text runs)
| PDF | pages | runs | ready | notes |
|---|--:|--:|--:|---|
| 13-page tagged export | 13 | 1,068 | 100% | 1 undecodable run |
| 72-page | 72 | 20,012 | 100% | parse 2.7 s total |
| 5-page | 5 | 1,870 | 100% | text lives in Form XObjects |
| 24-page (signed) | 24 | 2,167 | 93% | 130 undecodable, 28 fonts without width data |
| 12-page (signature anomaly) | 12 | 641 | 100% | |
| 4-page (signed) | 4 | 1,395 | 93% | 92 runs share an operator with neighbours |
| 2-page QCR | 2 | 521 | 100% | |

## Decision
Continue with the in-house engine as the primary implementation (best fidelity measured, no licence cost, fully local).
Do **not** adopt PyMuPDF (copyleft/commercial licence; weaker fidelity measured). Keep the editor behind one interface
so Apryse or Nutrient can be dropped in later.

**Blocker recorded:** Apryse and Nutrient fidelity cannot be benchmarked without trial/commercial keys; they must be
obtained by the stakeholder/owner. Until then no SDK is committed to.

## What stress testing on the real files taught us (M3)
Each of these was found by the independent verifier refusing a save, then fixed and pinned by a test:
* **Marked-content `/ActualText`** overrides what extractors read; an edit inside such a span must drop it, otherwise
  search/copy/accessibility keep returning the *old* wording.
* **Shared Form XObjects** (tiled exports reuse one giant form on every page, and a form can be drawn twice on a page):
  edits are copy-on-write (clone the form + resource dicts per Do site) so other pages never change.
* **Invisible text** (off-page or clipped) is never offered: runs are filtered by page box and by rendered ink.
* **Width model must use the real horizontal em** (includes `Tz` and the CTM) and `Tc`; nominal font size gave wrong fits.
* **A `ToUnicode` entry is not proof a glyph exists.** Presence is read from the embedded TrueType program
  (fontTools) and from codes actually used in page content; a render-ink check verifies every new character drew.
* **pypdf `compress_identical_objects(remove_orphans=True)` silently dropped images** (11 of 29 in one file). Not used;
  the `images_preserved` check would have refused the save anyway.
* Standard-14 fonts with no `/Widths` use exact AFM metrics (reportlab) instead of being refused.

Results (shortening, number-change and multi-edit-batch stress on CAR/Fire/GMC/Marine/QCR pages): all accepted edits
verified; refusals were honest `overflow` cases. Missing glyphs fall back to a *disclosed* standard-14 font
(sans/serif/mono, bold/italic from the original name); characters outside WinAnsi (e.g. ₹) are refused.

## Known limits (reported to users as plain sentences)
* New characters missing from the original subset font are redrawn in a similar standard font and the save says so;
  characters outside WinAnsi are refused (`glyph_unavailable`). An *embedded* substitute (for ₹ etc.) is open work.
* Edits are single lines. Wrapped paragraphs and table cells are edited line by line; reflow is refused.
* Text is left-aligned at the original start; centred/right-aligned text whose width changes is flagged.
* Fonts without width data that are not standard-14 are refused.
* Outlines of CFF (OpenType) subsets can't be inspected; the post-render ink check is the safeguard for them.
* Digitally signed PDFs: original preserved; editing only via a labelled unsigned working copy (M3).
