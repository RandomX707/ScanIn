# M0 XLSX Implementation Report

## Result

Implemented the standalone XLSX proof engine in the supplied `Channel360` workspace. The package inspects workbooks, lists editable and rejected cell targets, freezes patch plans, preflights patches, performs targeted OOXML edits, and verifies candidates independently against the frozen plan. It does not use openpyxl to write or resave a workbook.

The successful synthetic round trip is labelled `VERIFIED` by the independent verifier. The formula-dependent round trip is labelled `INCOMPLETE`; known dependents are listed, unknown dependencies are reported, and recalculation on open is requested. The verifier now uses its own ZIP/XML parsing and plan-digest implementation; adversarial edits outside the frozen authorization are blocked. The fixture generator now creates a deterministic standards-compliant 64×64 PNG and tests its CRCs and decompression. **M0 is PASS for the standalone acceptance scope**, including user-supplied LibreOffice Calc 25.2.3.2 import evidence for the five regenerated files. The on-sheet image anchor is 1×1 pixel and recognizable appearance was not verified. Standalone XLSX-1 no-op behavior passes; XLSX audit-event wiring is deferred to M3 (audit chain foundation M1).

## Files added

- `packages/core/models/contracts.py`: immutable typed values, patches, plans, permissions and verification results.
- `packages/core/adapters/xlsx/adapter.py`: OOXML inspection, target discovery, conservative formula dependency analysis, preflight support and targeted application.
- `packages/core/planning/planner.py`: stale-source, target, fingerprint, expected-value and type preflight; deterministic frozen plan hash and change manifest.
- `packages/core/verification/independent.py`: fail-closed verifier with its own `zipfile`/`xml.etree` package parser and plan-digest check; openpyxl is used separately for value/type read-back. It receives allowed changes only from the frozen plan.
- `scripts/m0_xlsx_demo.py`: `inspect`, `targets`, `plan`, `apply`, `verify` and `demo` CLI commands. `apply` refuses to write over its source path or inode.
- `tests/conftest.py`, `tests/unit/`, `tests/integration/`, `tests/adversarial/`: synthetic fixtures, PNG validity tests, unsafe-target, stale-patch, immutable-plan, preservation, verifier-independence and adversarial-tampering tests. The full suite has 48 passing tests.
- `requirements.txt`: pinned direct and transitive dependencies.
- `artifacts/m0/`: original and edited synthetic XLSX files, frozen plans, CLI outputs, JSON verification reports, acceptance results and pytest JUnit XML.
- `docs/M0_IMPLEMENTATION_PLAN.md`, `docs/M0_VERIFICATION_REPORT.md`, `docs/M0_LIMITATIONS.md`.

The authoritative architecture specification was not modified. The sibling `INSYGHT` Git checkout was left untouched. Git initialization at Channel360 was denied by the workspace filesystem, so no files are staged and no commit or push was made; setup instructions are in [M0_GIT_BASELINE.md](M0_GIT_BASELINE.md).

## Core behavior

- Strings become target-local inline strings. Shared-string tables are unchanged, so other cells using the same entry retain their value.
- Numbers are written as numeric XML values and limited to 15 significant digits. Booleans and dates use the corresponding native OOXML forms. Dates use the workbook's 1900 or 1904 epoch.
- Existing style attributes and `styles.xml` stay unchanged.
- ZIP member order and compression methods are retained. Uncompressed contents of all unrelated package parts are compared byte for byte.
- Formula references support A1 cells and ranges, absolute references, cross-sheet references and resolvable defined names. Unsupported expressions such as `INDIRECT`, `OFFSET`, external and structured references, and array formula dependency cases are treated as unknown.
- Provenance is written as a custom document property and checked against the source SHA-256, frozen plan hash, adapter identity and supplied actor/edit-set metadata.
- The verifier checks archive/XML reopening, target value and type through openpyxl, original styles, every changed part against plan permissions, target-cell XML boundaries, provenance, shared strings and formula dependencies.
- A no-op returns `NO_CHANGE` with no candidate bytes. The CLI does not create a derived file in that case.

## Dependencies and licenses

Installed and pinned with package metadata inspected: lxml 5.4.0 (BSD-3-Clause), openpyxl 3.1.5 (MIT), pytest 8.3.5 (MIT), et-xmlfile 2.0.0 (MIT), iniconfig 2.3.1 (MIT), packaging 26.3 (Apache-2.0 OR BSD-2-Clause), and pluggy 1.7.0 (MIT). No AI dependency is used.

## Reproduction commands

From the `Channel360` workspace:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/m0_xlsx_demo.py demo
.venv/bin/python -m pytest -q --junitxml=artifacts/m0/pytest-results.xml
```

The full CLI sequence can be reproduced from the generated sample inputs:

```sh
.venv/bin/python scripts/m0_xlsx_demo.py inspect artifacts/m0/m0_original.xlsx
.venv/bin/python scripts/m0_xlsx_demo.py targets artifacts/m0/m0_original.xlsx
.venv/bin/python scripts/m0_xlsx_demo.py plan artifacts/m0/m0_original.xlsx \
  --patches artifacts/m0/m0_patches.json \
  --provenance artifacts/m0/m0_provenance.json \
  --output artifacts/m0/m0_cli_patch_plan.json
.venv/bin/python scripts/m0_xlsx_demo.py apply artifacts/m0/m0_original.xlsx \
  --plan artifacts/m0/m0_cli_patch_plan.json \
  --output artifacts/m0/m0_cli_edited.xlsx
.venv/bin/python scripts/m0_xlsx_demo.py verify artifacts/m0/m0_original.xlsx \
  artifacts/m0/m0_cli_edited.xlsx \
  --plan artifacts/m0/m0_cli_patch_plan.json \
  --output artifacts/m0/m0_cli_verification.json
```

## Acceptance status

See [machine-readable acceptance results](../artifacts/m0/acceptance-results.json) and the [final validation report](M0_FINAL_VALIDATION_REPORT.md). The suite has 48 passing tests, no failures and no skips. XLSX-6 passes with the reported manual native-open evidence. Formula-dependent outputs remain `NOT_VERIFIED` and that scenario remains `INCOMPLETE`. Database audit-event wiring is deferred to M3 and is not treated as an M0 engine failure.

## M1 readiness

M0 is closed at PASS for its standalone acceptance scope. Excel was not tested; the native-open evidence is user-reported LibreOffice Calc 25.2.3.2 validation. The 1×1-pixel anchor means recognizable image appearance was not checked. The plan at [M1_IMPLEMENTATION_PLAN.md](M1_IMPLEMENTATION_PLAN.md) is preparatory only; no M1 implementation was started. Audit chain infrastructure is an M1 requirement, while M0 XLSX audit-event wiring is deferred to M3.
