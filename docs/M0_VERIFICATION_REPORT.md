# M0 XLSX Verification Report

## Latest automated run

```sh
.venv/bin/python -m pytest -q --junitxml=artifacts/m0/pytest-results.xml
```

**48 passed, 0 failed, 0 skipped.** Machine-readable results: [pytest-results.xml](../artifacts/m0/pytest-results.xml) and [acceptance-results.json](../artifacts/m0/acceptance-results.json). There were 30 openpyxl DrawingML read-back warnings. The writer does not save through openpyxl.

## Fixture image validation

`tests/unit/test_fixture_png.py::test_generated_fixture_png_has_valid_chunks_and_decodable_rgb_pixels` validates PNG signature, chunk bounds/order and CRCs, IDAT decompression, scanline shape/filter, and distinct shield/check colors. The generated image is deterministic, RGB8, 64×64 and uses standard-library encoding only.

## Round-trip and preservation results

- Safe scenario: `artifacts/m0/m0_verification.json` and `artifacts/m0/m0_cli_verification.json` — **VERIFIED**.
- Formula-dependent scenario: `artifacts/m0/m0_incomplete_verification.json` — **INCOMPLETE**; formula-dependent results are **NOT_VERIFIED**.
- `tests/integration/test_m0_png_preservation.py` verifies the original and edited PNGs are valid and byte-identical, charts/drawing parts and shared strings are unchanged, safe output is VERIFIED, dependent output remains INCOMPLETE, and source bytes are unchanged during each apply.
- `tests/adversarial/test_verifier_tampering.py` has 11 tests; unauthorized modifications to a valid image and other protected content are BLOCKED. The independent reader-boundary test also passes.

## Reports, hashes, and native compatibility

The demo regenerated XLSX files, frozen plans and JSON verification reports under `artifacts/m0/`. [sha256.txt](../artifacts/m0/sha256.txt) includes the original, demo-edited, CLI-edited, dependent original and dependent-edited workbook; every hash was rechecked successfully.

The user supplied independent manual validation of all five regenerated workbooks in LibreOffice Calc **25.2.3.2**: all imported with no PNG CRC errors; ZIP/XML integrity, package preservation, shared strings, embedded image bytes, chart bytes and intended changes passed. XLSX-6 native-open therefore **PASS** for the standalone M0 scope. This is user-reported evidence, not a test performed by this agent. Excel was not tested.

The previous manual report for the old fixtures, which had an invalid PNG, remains archived at [prior-invalid-png-2026-11-06](../artifacts/m0/prior-invalid-png-2026-11-06/). The new valid image is anchored at 1×1 pixel in the sheet; recognizable visual appearance was not verified. Formula-dependent output remains **INCOMPLETE**, with dependent-result verification **NOT_VERIFIED**, regardless of LibreOffice recalculation on open.

## Gate interpretation

All standalone M0 gates, including XLSX-6's native-open requirement, are **PASS** for the M0 scope. Overall result: **PASS**. The 1×1-pixel image anchor limits visual appearance evidence but does not negate reported file import or package preservation. Database-backed audit infrastructure is part of M1; XLSX pipeline audit-event wiring is deferred to M3 and is not an M0 engine failure. Formula recalculation remains separate from native file-opening compatibility and is never reported VERIFIED for the dependent scenario.
