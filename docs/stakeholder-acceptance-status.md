# Stakeholder corpus acceptance — status

Generated from private runs (per-file detail with names is kept in the ignored `.private/inventory/` folder).
Run with `scripts/dev/start-test-stack.sh` then `.venv/bin/python scripts/corpus/acceptance_run.py`.

**Result of the last full run: 15 PASS, 3 SAFE RESTRICTION, 0 FAIL** (17 documents + ZIP import).
Backend 630 passed / 32 skipped (DB-env baseline); browser suite 57 passed.

| Group | Verdict | What was verified |
|---|---|---|
| 4 unsigned text PDFs | PASS | real text edit (original glyphs removed, original font reused), preview of the modified PDF, save as next version, independent re-open + searchable text + pixel confinement, v1 byte-identical, lineage, audit event, edit of a historical version, overflow refused |
| 3 signed / anomalous PDFs | SAFE RESTRICTION | imported, read, understood, downloadable; edits blocked with a specific reason (signature structure present; one has a ByteRange that runs past the end of the file). Cryptographic trust is **not** validated. |
| 9 XLSX | PASS | faithful grid (merges, widths, heights, styles, picture), formulas shown and locked, blank template fields editable, only edited cells change, formulas/merges/images preserved, formula-dependent saves reported INCOMPLETE |
| 1 DOCX | PASS | blocks + table shown, bounded edits, table/images/links/headers preserved, Track Changes setting preserved and disclosed |
| ZIP | PASS | all entries recognised (7 of 7, 17 of 17), paths and Unicode names preserved, duplicates linked, hostile archive contained, malware archive refused, audit chain verifies |

## Open items (not claimed as done)
* Digital-signature **cryptographic** validation, and a product policy for an "unsigned working copy" of signed PDFs.
* New characters outside the original font and outside Windows-1252 (e.g. the rupee sign) are refused; an embedded
  substitute font is not implemented.
* PDF edits are line/cell level; wrapped-paragraph reflow is refused rather than attempted.
* DOCX: Original preview is an extracted/approximate rendering (not Word-accurate), multi-run paragraphs are read-only,
  blank DOCX table fields are not reported, and edits are not authored as Word tracked changes.
* XLSX: pictures are preserved, not editable; no recalculation engine (cached formula values stay stale and are disclosed).
* Apryse / Nutrient were not benchmarked: both need trial/commercial keys (see `docs/pdf-editing-engine.md`).
* No LLM summaries: any use needs explicit, privacy-reviewed approval.
