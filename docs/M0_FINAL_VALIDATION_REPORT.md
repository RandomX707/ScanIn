# M0 Final Validation Report

## Decision

**PASS for the standalone M0 acceptance scope.** Automated gates pass on regenerated fixtures and the user supplied independent native validation results for the five regenerated XLSX artifacts using LibreOffice Calc 25.2.3.2. All five imported successfully without PNG CRC errors; XLSX-6's required native-open check is satisfied. The image is encoded as a recognizable 64×64 PNG but drawn at 1×1 pixel on the worksheet, so recognizable on-sheet appearance was not verified. No M1 work was started.

Database-backed audit wiring is explicitly deferred to M3 XLSX pipeline integration under the existing milestone scope. It is not counted as an M0 standalone engine failure.

## Root cause and remediation

`tests/conftest.py` had embedded a fixed 68-byte 1×1 base64 PNG. Its IDAT chunk declared CRC `0xefa3e599`, while recomputation gave `0xefa2a75b`. IHDR/IEND CRCs were valid, so ZIP and XML checks did not detect the PNG payload defect. The same bad bytes appeared in original and edited workbooks because the writer correctly preserved the image.

The fixture now generates a deterministic 64×64 RGB PNG with Python standard-library `struct` and `zlib`: light background, blue shield, white interior and green check mark. The test validates the PNG signature, chunk bounds/order, all chunk CRCs, zlib decompression, scanline lengths/filter bytes, and expected image colors. No writer or verifier behavior was changed.

## Files changed

- Fixture source: `tests/conftest.py`.
- New PNG integrity test: `tests/unit/test_fixture_png.py`.
- New safe/dependent image, chart, drawing and shared-string preservation tests: `tests/integration/test_m0_png_preservation.py`.
- Adversarial valid-image tampering coverage: `tests/adversarial/test_verifier_tampering.py`.
- Updated reports: this file, `docs/M0_VERIFICATION_REPORT.md`, `docs/M0_LIMITATIONS.md` and `artifacts/m0/acceptance-results.json`.
- Regenerated artifacts: original and edited workbooks, frozen plans, inspect/target outputs, patch/provenance JSON, independent verification reports, pytest JUnit XML and SHA-256 manifests under `artifacts/m0/`.
- Archived old evidence: `artifacts/m0/prior-invalid-png-2026-11-06/`.

The XLSX adapter/writer and v2.1.1 specification were not modified.

Previous evidence and artifacts were retained under [prior-invalid-png-2026-11-06](../artifacts/m0/prior-invalid-png-2026-11-06/). Its original four workbook hashes validate against the archived SHA manifest.

## Automated results

Command:

```sh
.venv/bin/python -m pytest -q --junitxml=artifacts/m0/pytest-results.xml
```

Result: **48 passed, 0 failed, 0 skipped**. The suite emitted 30 existing openpyxl DrawingML warnings during read-back; the XLSX writer does not use openpyxl to save. The adversarial suite contains 11 passing tests, including modification of an otherwise valid embedded image.

The regenerated CLI edit independently reproduces the candidate SHA-256, and the source hash before and after a repeated CLI apply is identical. Integration tests also assert source-byte stability during `apply`.

## M0 acceptance gates

| Gate | Status | Exact test(s) | Evidence / scope |
| --- | --- | --- | --- |
| XLSX-1 | **PASS** | `tests/integration/test_m0_acceptance.py::test_xlsx_1_and_8_empty_or_equal_edits_produce_no_derived_file`; `tests/unit/test_cli.py::test_cli_noop_does_not_create_a_candidate_file` | No-op returns no derived file and source remains unchanged. Database audit event deferred to M3. |
| XLSX-2 | **PASS** | `tests/integration/test_m0_acceptance.py::test_xlsx_2_one_of_two_shared_string_references_changes` | Only selected cell changes; unrelated shared-string reference/value and sharedStrings part remain unchanged. |
| XLSX-3 | **PASS** | `tests/integration/test_m0_acceptance.py::test_xlsx_3_currency_date_boolean_round_trip_and_style_is_unchanged` | Typed values and style preserved in 1900 and 1904 systems. Programmatic check. |
| XLSX-4 | **PASS** | `tests/integration/test_m0_acceptance.py::test_xlsx_4_known_and_cross_sheet_dependents_are_incomplete`; `tests/integration/test_m0_acceptance.py::test_xlsx_9_indirect_dependency_is_unknown_and_never_verified` | Known cross-sheet dependencies are listed, unknown dependencies remain unknown, recalculation is requested and outcome stays INCOMPLETE. |
| XLSX-6 | **PASS** | `tests/integration/test_m0_acceptance.py::test_xlsx_6_chart_image_comments_validations_formatting_and_relationships_preserved`; `tests/integration/test_m0_png_preservation.py::test_safe_round_trip_preserves_valid_png_chart_relationships_and_shared_strings`; manual native check recorded below | Programmatic package/image preservation PASS. User-reported LibreOffice Calc 25.2.3.2 import of all five regenerated workbooks passed; no PNG CRC errors. Image's 1×1-pixel sheet anchor prevents a claim that its recognizable appearance was visually checked. |
| XLSX-8 | **PASS** | `tests/integration/test_m0_acceptance.py::test_xlsx_1_and_8_empty_or_equal_edits_produce_no_derived_file` | Empty edit creates no candidate or provenance part. |
| XLSX-9 | **PASS** | `tests/integration/test_m0_acceptance.py::test_xlsx_9_indirect_dependency_is_unknown_and_never_verified` | INDIRECT dependency is unknown; result is INCOMPLETE, not VERIFIED. |
| Frozen PatchPlan | **PASS** | `tests/adversarial/test_verifier_tampering.py::test_frozen_plan_permission_mutation_is_blocked`; existing stale-source/fingerprint unit tests | Plan tampering is BLOCKED; stale plan rejected. |
| Independent verification | **PASS** | `tests/adversarial/test_verifier_tampering.py::test_verifier_does_not_call_writer_parsing_or_fingerprint_helpers` | Verifier passes valid candidate with writer parser/fingerprint helpers patched to raise. |
| Adversarial protection | **PASS** | `tests/adversarial/test_verifier_tampering.py` (11 tests) | Unauthorized sheet cell, SST, formula, style, relationship, image, chart, provenance and permission modifications are BLOCKED. |

## PNG and preservation evidence

The regenerated PNG in each original and candidate was checked with the independent test parser. Each is a decodable 64×64 RGB8 non-interlaced PNG with valid IHDR/IDAT/IEND CRCs and zlib-decodable scanlines. PNG SHA-256 for all generated files is `c2300b7063c3c004059e3dcc261e3ddfe409d9f38a5c01cd7b4a7fcb54d3d228`.

`test_safe_round_trip_preserves_valid_png_chart_relationships_and_shared_strings` confirms:

- Original and edited image bytes are identical and both pass PNG validation.
- Chart, drawing XML, drawing relationships, and sharedStrings bytes are unchanged.
- The safe edit verifies **VERIFIED**.
- The original source bytes remain unchanged during apply.

`test_formula_dependent_round_trip_preserves_valid_png_and_remains_incomplete` confirms image preservation/validity while dependent-result verification stays **NOT_VERIFIED** and overall outcome **INCOMPLETE**. This is independent of the application's native ability to open the file.

## Regenerated workbook hashes

The current [sha256.txt](../artifacts/m0/sha256.txt) was rechecked with `shasum -a 256 -c`; every listed workbook passed. The before/after validation manifests are identical. `m0_edited.xlsx` and `m0_cli_edited.xlsx` are deterministic byte-identical outputs for the same source, frozen plan and provenance.

```text
m0_original.xlsx:           5dd123f479a3b9b1e66ec5542dd1c13d095500c258e0ae51d2ee1deb8b52d706
m0_edited.xlsx:             9b4ae72898ebe1e0f069fb564a5188a84208588122cbee129cc68a52b4251c02
m0_cli_edited.xlsx:         9b4ae72898ebe1e0f069fb564a5188a84208588122cbee129cc68a52b4251c02
m0_dependent_original.xlsx:d2e389afe3361693f9500d246c6b7512dd10192b3f3144d82766b017c8045d0a
m0_dependent_edited.xlsx:  eef656a7e8430aab0672a9492105ae635f152c25cd1d8a1308006dc88ee14af9
```

## Native application evidence

**User-supplied manual evidence: PASS.** The user reports opening all five regenerated workbooks in LibreOffice Calc **25.2.3.2**. All five imported successfully, with no PNG CRC errors. ZIP/XML integrity, package preservation, shared strings, embedded image bytes, chart bytes and intended changes passed. This closes XLSX-6's native-open requirement for the M0 acceptance scope. This is reported manual evidence; it was not executed by this agent. Microsoft Excel was not tested.

The supplied independent report, [M0_INDEPENDENT_VALIDATION_REPORT.md](../M0_INDEPENDENT_VALIDATION_REPORT.md), records LibreOffice Calc **25.2.3.2** headless import and PDF rendering of the prior four workbooks. They all imported, but all produced `libpng error: IDAT: CRC error`; those results validate the old package/open behavior only and do not demonstrate a valid rendered image. Excel and interactive GUI repair-dialog behavior were not tested in that report.

The user reports that LibreOffice recalculated the regenerated formula-dependent workbook on open. The earlier pre-fix report recorded a displayed dependent total changing from `376.5` to `601.5` and chart scales changing during PDF export; that particular numeric observation applies to the old fixture. Recalculation on open is **not** verification of every formula dependency or stored formula result. The regenerated dependent workbook's verifier outcome remains INCOMPLETE.

The PNG itself decodes and has valid chunk CRCs. The embedded picture is anchored at only 1×1 pixel, however, so recognizable on-sheet image appearance was not verified. This is recorded as a visual fixture limitation, separate from successful native opening and exact image-byte preservation. `m0_edited.xlsx` and `m0_cli_edited.xlsx` are byte-identical outputs, but all five named artifacts were included in the reported manual open check.

## Formula status and limitations

- Safe value edit verification: **VERIFIED**.
- Formula-dependent edit verification: **INCOMPLETE**; dependent results **NOT_VERIFIED**. LibreOffice recalculated on open, but that does not establish all dependencies or stored cached results.
- Native import compatibility: **PASS** by user-supplied LibreOffice Calc 25.2.3.2 results. Excel compatibility was not tested.
- Recognizable on-sheet image appearance: **NOT_VERIFIED** because its drawing anchor is only 1×1 pixel.
- Fixture set is synthetic and small; it is not the v2.1.1 50-file producer corpus.
- Formula analysis remains conservative for unsupported/ambiguous constructs.
- Database audit persistence is not in the standalone M0 engine; XLSX event wiring is deferred to M3.

## Reproduction commands

```sh
.venv/bin/python scripts/m0_xlsx_demo.py demo --outdir artifacts/m0
.venv/bin/python scripts/m0_xlsx_demo.py plan artifacts/m0/m0_original.xlsx --patches artifacts/m0/m0_patches.json --provenance artifacts/m0/m0_provenance.json --output artifacts/m0/m0_cli_patch_plan.json
.venv/bin/python scripts/m0_xlsx_demo.py apply artifacts/m0/m0_original.xlsx --plan artifacts/m0/m0_cli_patch_plan.json --output artifacts/m0/m0_cli_edited.xlsx
.venv/bin/python scripts/m0_xlsx_demo.py verify artifacts/m0/m0_original.xlsx artifacts/m0/m0_cli_edited.xlsx --plan artifacts/m0/m0_cli_patch_plan.json --output artifacts/m0/m0_cli_verification.json
.venv/bin/python -m pytest -q --junitxml=artifacts/m0/pytest-results.xml
shasum -a 256 -c artifacts/m0/sha256.txt
```

## Git baseline

The project root is `/Users/yashaswiram/scalehealth/pocs2/Channel360`; the v2.1.1 spec, M0 code, tests and generated artifacts are in this directory. Git initialization was attempted for the requested M0 baseline but the workspace denied creation of `Channel360/.git` with `Operation not permitted`. No files were staged and no commit or push was made. The current ignore file excludes `.venv/`, Python bytecode and pytest cache. See [M0_GIT_BASELINE.md](M0_GIT_BASELINE.md) for the exact setup commands to run in a writable workspace. `INSYGHT` was not modified.

M0 is **PASS** for the standalone acceptance scope. No M1 implementation was started. Database-backed audit event wiring is deferred to M3 and does not block M0. Formula outputs remain unverified by design, and the image's 1×1-pixel anchor limits visual evidence without failing XLSX-6's native-open requirement.
