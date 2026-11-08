# M0 XLSX Proof Spike — Implementation Plan

## Scope

Implement only the standalone XLSX inspect, targets, immutable patch plan, targeted OOXML apply, independent verification, CLI demo, synthetic fixtures, and M0 documentation. Use Python 3.12 with pinned `lxml`, `openpyxl`, and `pytest`; no services or AI dependencies.

## Work sequence

1. Define typed contracts, structured preflight errors, deterministic source fingerprints, and the frozen `PatchPlan`.
2. Build XLSX inspection and target discovery from OOXML, including shared strings, formulas, styles, images, charts, unsupported parts, rich text, array formulas, and conservative formula dependencies.
3. Implement preflight and byte-preserving ZIP rewrite that patches only authorized worksheet cells and required calc flags; never resave the workbook through openpyxl.
4. Implement independent verification against the frozen plan, checking ZIP/XML integrity, intended values and types, every unrelated uncompressed part, authorized XML changes, relationships/styles/shared-string isolation, provenance, and formula dependents.
5. Generate synthetic fixtures and tests for scenarios A–G, PatchPlan freezing, verifier boundary, and acceptance gates XLSX-1/2/3/4/6/8/9.
6. Add CLI commands and deterministic original/edited artifacts, run the M0 pytest suite, and write implementation, verification, and limitations reports with hashes and gate results.

## Design calls

- Workspace is the supplied spec directory because it is the only content in the named `Channel360` workspace; the sibling `INSYGHT` Git checkout is unrelated and remains untouched.
- Preserve source ZIP member order and compression methods. ZIP container bytes may differ, while uncompressed unrelated member content must match exactly.
- Use inline strings for edited string cells; preserve style attributes and all other cell attributes.
- Keep frozen plan contents independent from candidate output. Embed provenance in `docProps/custom.xml`; permit the corresponding content-type and package-relationship changes required by the specification.
- Return `NO_CHANGE` before creating an output for empty/equal-value patch sets.
- A known or unknown formula dependent forces recalculation on open and yields `INCOMPLETE`.
