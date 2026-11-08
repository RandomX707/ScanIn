# Insurance Document Workbench — Independent M0 Native Validation

> Historical evidence for the pre-fix invalid-PNG fixtures only. Superseded for current M0 status by [docs/M0_FINAL_VALIDATION_REPORT.md](docs/M0_FINAL_VALIDATION_REPORT.md), which records the user-supplied LibreOffice Calc 25.2.3.2 validation of all regenerated workbooks.

**Validation date:** 2026-11-06
**Validator environment:** Linux, LibreOffice Calc 25.2.3.2 (headless import + PDF rendering); ZIP/XML structural checks and independent XML diffs.
**Input files:** `m0_original.xlsx`, `m0_cli_edited.xlsx`, `m0_dependent_original.xlsx`, `m0_dependent_edited.xlsx`.

## Verdict

**CONDITIONAL PASS — XLSX native import and safe-edit preservation demonstrated, but the image portion of XLSX-6 cannot be cleared because the original fixture contains a corrupt embedded PNG.** This is not evidence of an XLSX editor regression: the same exact invalid image bytes appear in all four packages. Tests in Microsoft Excel were not performed, nor was interactive GUI opening or an inspection of Excel repair dialogs.

## File integrity

| File | SHA-256 | ZIP CRC test | XML parts parse | LibreOffice Calc import |
|---|---|---|---|---|
| `m0_original.xlsx` | `e861e9ddc88d7fdb13645ca8ac5babbfdc24a21134302e2fa159a461c83e3363` | PASS | PASS | PASS with PNG warning |
| `m0_cli_edited.xlsx` | `a04621f121a185652107b13f178913c0c2583942c2928263a37389bdc93eb5da` | PASS | PASS | PASS with PNG warning |
| `m0_dependent_original.xlsx` | `834a783e52fbffaf5310e399f0fbbc4d99c974fb53757d9dac1cac2734bc46c2` | PASS | PASS | PASS with PNG warning |
| `m0_dependent_edited.xlsx` | `22b196bd8a14279cab6cae486d5ef2ab2eb1b314109153cbc889bae9a5a468ca` | PASS | PASS | PASS with PNG warning |

LibreOffice imported each XLSX and successfully rendered a three-page PDF (`exit=0`). Import emitted `libpng error: IDAT: CRC error` on **all four files**.

## Safe edit: `m0_original.xlsx` → `m0_cli_edited.xlsx`

- Cell `Claim!G2`: `constant` → `confirmed` (source and output confirmed directly in `xl/worksheets/sheet1.xml` and in LibreOffice-rendered text).
- When `G2` is replaced with the same placeholder in both parsed XML documents, the canonicalized remaining worksheet XML is identical.
- `xl/sharedStrings.xml`, `xl/styles.xml`, `xl/charts/chart1.xml`, `xl/media/image1.png` and all 15 pre-existing unmodified ZIP members retain identical uncompressed bytes.
- The only changed existing parts are `xl/worksheets/sheet1.xml`, `[Content_Types].xml`, and `_rels/.rels`; `docProps/custom.xml` was added for provenance.
- LibreOffice renders three pages in both. Rasterized PDF pages 2 and 3 are pixel-identical; page 1 differs only within a small text bounding box around the intended replacement.
- **Assessment:** edit intent and package/content preservation PASS; LibreOffice import PASS. The PNG image is not a valid test of visual preservation because it is broken in the source.

## Formula-dependent edit: `m0_dependent_original.xlsx` → `m0_dependent_edited.xlsx`

- Cell `Claim!D2`: numeric value `125.5` → `200.5`, style ID `s="1"` preserved.
- The worksheet outside `D2` is identical under canonicalization; style, shared-strings, chart and media parts are byte-identical.
- `xl/workbook.xml` changes the calculation flags from `fullCalcOnLoad="0"` to `fullCalcOnLoad="1"` and adds `forceFullCalc="1"`.
- LibreOffice recalculates displayed dependent results during PDF export: a visible total changes `376.5` → `601.5` and chart scales change accordingly.
- **Assessment:** native import PASS; intended data change PASS; conservative `INCOMPLETE` classification of formula-dependent verification remains correct. LibreOffice recalculation is *not* proof of future Excel recalculation or stored formula-cache correctness.

## Embedded image problem — action needed

All four files contain an identical 68-byte `xl/media/image1.png` with SHA-256:

`99f80e863bbb08c8bf5ff1b368a216929593c128ef878ea550b5f6b257976ec5`

It has a proper PNG signature and valid `IHDR` and `IEND` checksums, but its **IDAT CRC is invalid**:

- Recorded: `0xefa3e599`
- Recomputed: `0xefa2a75b`

This proves the image is already invalid in **both original fixtures**, and identical image bytes were preserved by the write-back engine. LibreOffice cannot render the embedded image and emits a decoding warning. This should not be attributed to the editor, but it invalidates any claim that the image portion of the native compatibility gate was visually validated.

**Recommended fix in the source repository:** regenerate the synthetic fixture with a correctly encoded, nontrivial PNG using a reputable image library; create fresh original/edited workbooks, repeat the M0 tests and native-open check, and record new SHA-256 hashes. Keep original fixtures for regression comparison. Add a fixture precondition that embedded images decode and have correct chunk checksums before testing preservation.

## Scope and limitations

1. LibreOffice **headless** import and PDF export succeeded; an interactive GUI test with repair-dialog observation was not performed.
2. Microsoft Excel was not available and was not tested.
3. Native render output shows the safe edit confined to the expected text region and no changes on other pages. Formula-dependent page changes are expected from recalculation.
4. No changes were made to the uploaded XLSX files. The PDF files and PNG snapshots created for analysis are independent derivatives only.
5. The workbook corpus is synthetic; these results do not demonstrate broad compatibility with real-world complex Excel files.

**Recommended M0 gate status:** native-open subcheck **PASS for LibreOffice headless import**, native visual image-preservation subcheck **NOT VERIFIED because of invalid source fixture**. Overall **CONDITIONAL PASS**, pending a valid-image fixture and, if required, a GUI-native inspection.
