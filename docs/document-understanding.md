# Document understanding (Chunks tab)

Rule-based and deterministic. No AI or model calls; nothing leaves the machine. Every string shown is lifted verbatim
from the document and carries its source location.

* **Policy PDFs** (`packages/core/policy_sections.py`): the sandboxed worker returns visible text as positioned lines
  (page, position, size, weight, cell segments). Headings are matched against a vocabulary (schedule, insured, coverage,
  sums insured, premium and taxes, add-ons, warranties, exclusions, conditions, clauses). One logical section per
  category that the document actually supports, with every occurrence anchored to a page; labelled values
  ("Policy No : …") become facts for the matching section. Documents with no matching headings fall back to their own
  major headings, then to the plain text chunker. The Chunks tab can jump to the cited page.
* **Spreadsheet templates** (`packages/core/sheet_sections.py`): headings are single text rows with no input cells
  beside them; label/value pairs become facts with cell references; empty template fields are reported as "awaiting
  input" and never filled in.
* **DOCX quotation forms**: all-caps or heading rows open sections; multi-cell table rows become label/value facts.
* **Verification**: `tests/corpus/test_stakeholder_understanding.py` checks on the private corpus that every heading,
  excerpt and fact appears on the page (or equals the cell) it cites.

Known limits: sections come from heading vocabulary and layout, so unusual wording can be missed (the fallbacks then
apply); values are read from one cell/line, so a value wrapped over several lines is cut at the line; scanned PDFs have no
text layer and are not understood; DOCX blocks do not expose empty cells, so blank DOCX fields are not reported.
