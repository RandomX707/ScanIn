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

## Known limits (reported to users as plain sentences)
* New characters must exist in the original (usually subset) font. Otherwise the edit is refused (`glyph_unavailable`);
  a disclosed, embedded substitute-font path is planned in M3.
* Edits are single lines. Wrapped paragraphs and table cells are edited line by line; reflow is refused.
* Text is left-aligned at the original start; centred/right-aligned text whose width changes is flagged.
* Fonts without width data (standard-14 without /Widths) are refused until width tables are added.
* Digitally signed PDFs: original preserved; editing only via a labelled unsigned working copy (M3).
