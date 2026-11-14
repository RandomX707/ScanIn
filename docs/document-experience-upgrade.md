# Channel360 — Document experience upgrade

Version comparison · Excel workspace · Word page rendering · Digitally signed PDF working copies

Status: implemented and final hardening/regression verified on an **isolated stack** (database `channel360_iso_harden`, storage
`.runtime/iso-final/storage`, fresh tenants). The shared application on `:8000` / `:5174` was not restarted or modified by this
work. Nothing in this document contains stakeholder content, extracted
field values, document copies or secrets; private evidence stays under `.private/` (git-ignored).

Workflow now supported end to end:

```
UPLOAD → VIEW FAITHFULLY → UNDERSTAND → EDIT → PREVIEW → SAVE NATIVE VERSION → COMPARE ANY TWO VERSIONS → DOWNLOAD
```

## 1. Architecture

All four features extend the existing engine. There is no new trust path: every new capability that touches an uploaded file
runs in an OS-sandboxed worker (the initial Seatbelt profile was verified; the tightened pre-push profile is not yet runtime-verified; Linux Bubblewrap command construction only, see §13) and every new endpoint
is tenant-scoped through `api_tenant_transaction` (PostgreSQL row-level security).

| Area | New / changed code | Sandbox |
|---|---|---|
| Common viewer | `components/PageViewer.tsx` (PDF.js pages: zoom, fit width, actual size, page navigation, full screen, highlights) | browser |
| Comparison | `packages/core/compare.py` (pure diff engine), `packages/api/compare.py` (snapshots, lineage, audit, endpoints), `pages/Compare.tsx` | snapshots come from the existing sandboxed parsers/PDF worker |
| Excel | `components/SheetWorkspace.tsx`, `pages/SheetGrid.tsx`, `lib/numfmt.ts`, `packages/core/xlsx_layout.py` | layout extraction runs in the sandboxed parser |
| Word | `packages/workers/office_render.py`, `packages/api/render.py`, DOCX adapter (`fmt`, `props`, `media`) | LibreOffice under Seatbelt |
| Signed PDFs | `packages/workers/sigcheck.py`, `packages/core/pdf_working_copy.py`, `packages/api/working_copy.py`, `components/SignaturePanel.tsx` | OpenSSL under Seatbelt; working-copy builder in the PDF worker |

The initial feature did not require a database migration (`0007_edit_set_supersession`). The pre-push hardening follow-up adds
`0008_version_lineage_class`; see §13. Initially, "Unsigned working copy" was derived from an audit payload flag and parent chain;
the follow-up moves this classification into durable version metadata.

### 1.1 Comparison engine

The engine compares **persisted version files**, never replayed edit events.

1. `snapshot()` reads each version through the existing sandboxed workers: PDF → visible text lines with page and position;
   XLSX → every cell value/formula (all targets, not just the 500×40 display window), resolved cell style, merged ranges,
   picture anchors/hash, widths/heights; DOCX → paragraph/table blocks with a formatting fingerprint and package media hashes.
   Snapshots are cached per `(tenant, version, sha256)`.
2. `diff_*` aligns the two snapshots (sequence alignment; a one-for-one replacement is a *modification*) and emits change
   records `{id, kind: added|removed|modified, category, location, before, after, bbox…}`.
3. The API adds **lineage**: `same`, `base_is_ancestor` (direct or cumulative via the real parent chain), `target_is_ancestor`,
   or `branch` (route up to the common ancestor then down). Consecutive version numbers are never assumed to be ancestry.
4. **Audit details** (actor, recorded time, verification outcome, modification type, parent) come only from the audit trail and
   version rows. When the trail has no entry the UI says "actor not recorded".
5. Limits are returned with every result (`limits[]`) so a text diff is never presented as a complete visual comparison
   (for example DOCX image *position* and shapes are not compared; PDF drawings/annotations are not compared).

Comparison endpoints are read-only; they cannot create, change or delete a document, version or storage object. They do not
expose file bytes (the existing `/file` download gate, including `verified_download_required`, is unchanged).

### 1.2 Excel workspace

`SheetWorkspace` wraps `SheetGrid` with sheet tabs, formula bar, zoom (50–200 %), fit-to-used-range, actual size, full screen
and selection that survives expanding. The grid draws row/column headers, frozen panes, hidden rows/columns, merged ranges,
fonts/colours/borders/fills/alignment/wrapping, **display-only** number and date formats, and pictures at their anchors.
Formula cells show the formula with an `fx` mark and are locked; protected cells are hatched. Nothing is recalculated or
invented. Editing still goes through the verified XLSX adapter; `INCOMPLETE` results (formula dependents not recalculated) keep
the existing download gate. The workbook is never regenerated from HTML or a screenshot.

### 1.3 Word page rendering

DOCX (and DOC/ODT/RTF/PPTX) are rendered to a **preview-only PDF** by LibreOffice, then shown with the same `PageViewer`.
The preview is never an editing source and never saved as a version; the save path stays DOCX → native DOCX.

Isolation: the document is copied into a private scratch directory; Seatbelt denies IP network, file writes outside the scratch
directory (plus system temp), and reads under `/Users`; a fresh LibreOffice profile disables macros and sets link/field updates
to "never"; wall-clock timeout, process-group kill, output-size and page-count caps; magic-byte check before conversion.
Renders are cached under `<storage>/.preview-cache/<tenant>/<version>-<sha256>.pdf`; the key contains the content hash, so a
changed version can never be served a stale render. If the renderer is unavailable, the Original tab shows the extracted text
under an explicit banner ("PAGE PREVIEW UNAVAILABLE · SHOWING EXTRACTED TEXT, NOT THE ORIGINAL LAYOUT").

Renderer benchmark (Health Quote Format.docx, primary fixture): LibreOffice (local, sandboxed) rendered 4 pages with logo,
illustration, brand colours, bullets and all 12 hyperlinks. A browser DOCX renderer (docx-preview 0.3.3, Apache-2.0) collapsed the
document into one tall page, clipped the wide table and drew bullet glyphs as empty boxes. LibreOffice was chosen. No cloud
renderer was considered (privacy rule).

### 1.4 Signed PDFs

Signature **status** is computed by `SignatureValidator` (OpenSSL CMS verify, sandboxed) and reports three separate things:

* **integrity** – do the signed byte ranges still match the signature (valid / invalid / malformed / unverifiable);
* **trust** – does the signer certificate chain to a CA in an *explicitly configured* trust file (`IDW_PDF_TRUST_CAFILE`).
  With no trust file the answer is always **"trust not established"**, never "trusted";
* **coverage** – whether the signature covers the whole file or content was appended after signing.

Aggregate states: `none`, `unsigned_signature_field`, `malformed`, `integrity_invalid`, `not_validated`, `modified_after_signing`,
`integrity_valid_untrusted`, `integrity_valid_trust_not_established`, `integrity_valid_trusted`. `validated` is true only for
integrity **and** a configured trust policy. Legal/regulatory acceptance is never claimed. A malformed container (for example a
`/ByteRange` that runs past the end of the file) is reported as malformed and never as trusted.

**Create editable working copy** (`POST …/working-copy`, owner only, explicit acknowledgement): the original is read only; a new
PDF is built in the sandboxed PDF worker by re-writing the file with every signature field, signature value, DocMDP permission,
SigFlags and document security store removed (a second pass keeps only objects reachable from the cleaned catalogue), a visible
strip "DRAFT — MODIFIED COPY — NOT OFFICIAL" plus a provenance line added in **new space above each page** (no original content
is covered or moved), and provenance metadata. The result is self-verified (no signature artefacts, same page count, notice on
every page, original page text preserved) and re-checked independently in the API process before it is committed as a new
version whose parent is the chosen version, with an audit event. Later edits of the copy are ordinary versions that inherit the
class; the notice strip is locked in the editor and edits that target it are refused. The UI shows a persistent banner and the
"Unsigned working copy · not official" label in the header, version pills, History and Compare.

Not implemented (by design, per policy): re-signing / newly-signed workflow; the system never signs, simulates a signature or
copies a signature into an edited document.

## 2. API additions

All under `/api`, bearer-authenticated, tenant-scoped; unknown or foreign ids return `404`, missing token `401`.

| Endpoint | Purpose |
|---|---|
| `GET /documents/{d}/versions/{v}/render` | Preview-only PDF pages of an Office document (`415` for unsupported types, `503 render_unavailable`, `422 render_failed`). Headers: `X-Render-Engine`, `X-Render-Cache`, `X-Render-Note`. |
| `GET /documents/{d}/compare?base=&target=` | Comparison of any two versions of the family: `format`, `base`/`target` cards, `relation`, `path`, `summary`, `changes[]`, `limits[]`, `note`, PDF `signature` + `pages`. |
| `GET /documents/{d}/versions/{v}/changes` | Comparison with the version's **direct parent** (`422 no_parent` for the original). |
| `GET /documents/{d}/history` | Per-version lineage class, outcome, download eligibility, actor/time, signature status. |
| `POST /documents/{d}/versions/{v}/working-copy` | Body `{"acknowledge": true}`. `422 acknowledgement_required`, `409 not_signed`, `409 already_working_copy`, `415 working_copy_pdf_only`, `403/401` non-owner. |

Changed responses: `preview` (DOCX `render`; PDF `signature{state,label,detail,validated,signatures[],docmdp,limits}`, `classification`,
`working_copy`), `folders` (`versions[].classification`), `chunks` (DOCX sections carry `source.pages` where the heading can be located),
XLSX `layout` (`freeze`, `zoom`, `hidden`, `protected`, style `nf`).

## 3. Configuration

| Setting | Default | Meaning |
|---|---|---|
| `IDW_SOFFICE_PATH` | `/Applications/LibreOffice.app/Contents/MacOS/soffice`, then `soffice` on `PATH` | Office renderer binary |
| `IDW_OPENSSL_PATH` | `/opt/homebrew/bin/openssl`, `/usr/local/bin/openssl`, then `PATH` (must be OpenSSL 3+, not LibreSSL) | CMS verification |
| `IDW_PDF_TRUST_CAFILE` | unset | PEM bundle of trusted signing CAs. Unset means every signer is "trust not established". |

## 4. Isolated test environment

* `scripts/dev/isolated-stack.sh up|down|status|db|drop` — own database `channel360_iso_*` (refuses any other name), own storage under
  `.runtime/iso-stack/`, ports `8010`/`5180`, own tenant. `db` provisions only a database (migrated, granted; used for the PostgreSQL
  integration tests via `C360_ISO_DB=channel360_iso_pgtests`). `C360_ISO_WEB_MODE=preview` serves a production build so frontend edits
  cannot hot-reload a running browser test. `drop` removes only a database it created and marked.
* `scripts/dev/iso_client.py` / `issue_iso_token.py` — fresh tenant + owner token inside the isolated database only.
* The Playwright suite, the acceptance runner and every `scripts/corpus` flow now default to the isolated stack and **refuse**
  `:8000`/`:5174`. `scripts/host/issue_test_token.py` (which wrote tenants into the shared database) refuses unless
  `C360_ALLOW_SHARED_DB_TENANTS=yes`. The PostgreSQL security tests may now also target `channel360_iso_*` databases.

## 5. Migration and deployment

* **Database:** none. No schema change, no data change, no reprocessing: sections, comparison, previews and signature status are
  computed on read; old documents work as soon as the backend runs the new code.
* **Packages:** none new (`pypdf`, `reportlab`, `pdfjs-dist` already present). The verified macOS host needs LibreOffice, OpenSSL 3+,
  PostgreSQL 16 and the configured malware scanner service. Seatbelt must be available for document workers.
* **Frontend:** served live by Vite on `:5174` (or `npm run build`); no new npm dependency.
* **Deploy:** no deployment was performed. Deployment is not ready for environments that require Linux/container confinement:
  those runtimes remain unverified. For the supported macOS deployment, back up database and storage, use the existing environment,
  ensure LibreOffice/OpenSSL 3+/PostgreSQL 16/scanner/Seatbelt are present, restart the backend, then verify `/openapi.json` lists
  the new paths. This run made no database migration.

## 6. Rollback

Code-only rollback; there is no schema migration to reverse. Stop the changed backend, restore the previously deployed backend
revision and its preserved environment, and keep the database and storage in place. The last confirmed remote revision before
this feature series is `1ba0813ac9a703c45747bf7cccb90f656ab7b4d8`; use the deployment's recorded prior revision if it differs.
Preview cache files are disposable. Saved native versions and unsigned working-copy versions remain ordinary immutable versions;
the earlier backend can list them without the newer UI metadata.

## 7. Verification results

Final hardening results below ran against the **fresh isolated stack** (database `channel360_iso_harden`, storage
`.runtime/iso-final/storage`, fresh tenants) on the real private stakeholder corpus. Counts only; no document content is reproduced.

| Suite | Result |
|---|---|
| Backend `pytest tests` with migration/app/worker roles on fresh `channel360_iso_backend` | **718 passed, 0 failed, 0 skipped** |
| Focused runner regressions (selection eligibility, explicit ZIP skip, isolated storage-reader guard, Excel numeric coercion) | **4 passed**; shared-formula and XLSX adapter targeted set: 52 passed |
| Playwright full suite, production build | **91 tests: 90 passed, 1 skipped, 0 failed**. The skip is the original QCR/RFQ ZIP import scenario; the hostile archive test passed. |
| Non-ZIP stakeholder acceptance (`--skip-zip`) | **17 documents, 279/279 checks pass**; ZIP recorded `SKIPPED — OUT OF SCOPE` (0 checks) |
| Version comparison flow: PDF/XLSX/DOCX, branch/ancestor/reverse, completeness and download gates | **31/31** |
| Additional XLSX/DOCX branch-history flow | **11/11** |
| Signed-PDF flow: Health, Motor, malformed Marine and unsigned control | **82/82** |
| New-endpoint security: auth roles, tenant isolation, signature tamper/fabrication, malformed and unsupported files | **23/23** |
| XLSX index fidelity vs independent openpyxl parse of 9 workbooks | **36/36** |
| DOCX fidelity and native edit/save/reopen, `Health Quote Format.docx` | **26/26** |
| Download eligibility tests (included in backend suite) | **5/5**; `INCOMPLETE` XLSX remains HTTP 409 |
| XLSX benchmark: small RFQ, medium QCR, 60k and 180k synthetic workbooks | **32/32 checks**; all four comparisons COMPLETE |

### 7.1 Repeatability of the browser test that failed once
One run of the full browser suite failed once (`manager-ui` DOCX upload: the workspace never appeared within 60 s) and passed on
re-run. A re-run is not treated as proof of stability, so the scenario was repeated under controlled conditions
(`e2e/repeat-docx-upload.spec.js`, every API call timed):

| Condition | Runs | Failures |
|---|---|---|
| Idle machine | 30 | 0 (workspace visible after ~1.07 s every time) |
| 10 CPU burners + 4 concurrent upload/render/sections loops | 30 | 0 (median 1.16 s, worst 5.3 s) |
| Vite dev server, a non-component source module edited every 3 s during the runs | 14 | **1** (dev server logged 62 page reloads) |
| Production build served by `vite preview`, the same edits | 14 | 0 |

Finding: the earlier DOCX upload timeout is reproduced by hot-reloading the dev server mid-test. In this final rerun, one
additional test assumption failed because a fresh local demo tenant correctly had no recent-document chips. The isolation
assertions showed no added demo documents; the test now accepts an empty recent row and checks the home page. The full production
suite then passed 90 tests with the single archive skip.

## 8. Measured fidelity

**Word (`Health Quote Format.docx`, LibreOffice preview vs the file's own XML):** page size error 0.01 pt (A4); text coverage
100 %; hyperlinks 12/12 present as real links; embedded images 2/2; logo block spans 48 % of the page width; illustration paints
12.3 % of its band; the DOCX-defined heading colour is rendered (3.1 % of pixels); bullets and the comparison table render;
4 pages. **The file defines no page background (no `w:background`), so the faithful rendering is a white page; a dark canvas
seen elsewhere comes from the viewer's own theme, not from the file.** Native edit: exactly one paragraph differs, media/styles/
numbering/theme/settings/headers/footers are byte-identical, the saved file re-renders to the same 4 pages, and the change touches
0.05 % of the pixels of one page.

**Excel (9 stakeholder workbooks plus two synthetic sizes):** the independent index comparison confirms every non-empty cell,
formula string, merge, picture anchor, column width and frozen pane (36/36). The production browser suite exercises sheet
navigation, zoom, fit width, scrolling, full screen, formula locking, selection persistence and edits for the real workbooks.
Acceptance independently opens saved XLSX files and checks edited cells. A numeric string entered into an empty cell may reopen
as an Excel number; edits to existing text must remain exact. Formula results are not recalculated.

**Signed PDFs:** Health and Motor: signature intact, signer **not trusted** (no trust policy configured). Marine: **malformed**
(byte range runs past the end of the file). A one-byte change inside a signed range reports "does not verify"; a fabricated
signature object is reported malformed; none is ever reported trusted. Working copies of all three are unsigned (no signature
structure of any kind), carry the notice on every page, keep original page text, and the signed originals stay byte-identical.

## 9. Performance (fresh isolated stack, idle machine)

| Workbook | Import+inspect | Index ready | First viewport | Scroll window p50 / max | Native save | COMPLETE compare | Cells examined |
|---|---:|---:|---:|---:|---:|---:|---:|
| Small RFQ (604 cells) | 0.28 s | 0.23 s | 9 ms | 4 / 9 ms | 0.61 s | 0.23 s | 604 |
| Medium QCR (312 cells) | 0.17 s | 0.22 s | 6 ms | 3 / 4 ms | 0.54 s | 0.22 s | 312 |
| Synthetic 60,000 (60,830 indexed cells) | 1.38 s | 1.74 s | 7 ms | 4 / 5 ms | 3.88 s | 1.84 s | 60,830 |
| Synthetic 180,000 (180,830 indexed cells) | 3.79 s | 4.79 s | 8 ms | 5 / 6 ms | 11.37 s | 5.02 s | 180,830 |

Each case saved two permitted text edits, independently reopened the native XLSX, and found both edits in a COMPLETE comparison.
The RFQ text selections were 53 and 49 characters long, exercising the former 40-character failure. The 60k and 180k comparisons
included cells beyond the initial viewport. Sampled API RSS before/after each case was 26.9→85.2 MB, 85.2→86.1 MB,
86.1→349.9 MB, then 349.9→225.1 MB. These are point samples, not peak memory; the final decrease shows memory reclamation,
while 349.9 MB was the largest observed sample.

Historical comparison: an earlier run reported about 8 seconds for the 180k initial index/view, 34 seconds for native save and
90 seconds for a comparison that examined only 16,000 cells and was marked incomplete. The final run measured 4.79 seconds to
index, 11.37 seconds to save and 5.02 seconds for a COMPLETE comparison of 180,830 cells. The older and current numbers are from
separate runs, not a same-process controlled benchmark.

Earlier pre-optimisation measurements reported `503` timeouts for 60k/180k workbooks and a 7.4-second import at 15k cells.
Adapter optimisations removed per-cell data-validation rescans and repeated whole-file hashing. The current end-to-end figures
include upload/inspection, index creation, viewport requests, save, native reopen and comparison.

## 10. Failures found during verification, and how each was resolved

| # | Finding | Resolution |
|---|---|---|
| 1 | XLSX inspection was quadratic; 60,000+ cells timed out (`503`) | Hoisted per-sheet work out of the per-cell loop (behaviour unchanged, all XLSX tests and acceptance pass) |
| 2 | Earlier large-workbook comparison stopped at 16,000 cells and could omit later changes | Current indexed comparison examines all 180,830 cells and reports `complete`; benchmark found both the first-window and far-row edits |
| 3 | Earlier 180k index/view and save took about 8 s and 34 s | Final isolated run measured 4.79 s index, 11.37 s save and 5.02 s complete comparison; no timeout was raised |
| 4 | An inherited audit helper had a hard-coded shared database URL and an old storage root | Replaced with an isolated-only database guard and configured `.runtime` storage; execution was verified against the fresh isolated database |
| 5 | My signed-PDF flow silently skipped the Marine edit check (that PDF stores one word per text object) | Missing editable text is now an explicit failing check; Marine edits and saves; 82/82 |
| 6 | A browser test failed once | Cause reproduced and mitigated (§7.1) |
| 7 | Several test-script defects (wrong selector scope, whitespace-normalised text, caret placement in a wrapped paragraph, page-1-only line search) | Fixed in the tests; no product behaviour was changed to make a test pass |
| 8 | Benchmark text selection could raise `IndexError` when short filters left fewer than three cells in a window | Selects non-empty strings with the API editable flag; tests cover strings over 40 chars and exclude formula/protected cells |
| 9 | Acceptance's native XLSX reopen assertion was unconditional (`or True`) | It now opens saved files independently and checks every edited value; Excel numeric coercion is accepted only for cells that were blank |
| 10 | A fresh-database browser test required a recent-document chip | It now accepts an empty demo workspace while still checking for leaked test documents |
| 11 | DOCX bullet fidelity assertion accepted every document because it tested `"" in text` | It now requires a supported bullet/list marker; the real DOCX run passed |
| 12 | Comparison/signature/security raw requests were pinned to port 8010 | They now use the isolated client's configured API URL and passed on port 8011 |
| 13 | An earlier `APIError(details=…)` call passed `details` positionally (500 instead of 422) in two new endpoints | Fixed; covered by tests |

## 11. Known limitations and unresolved blockers

The entries below describe the original implementation verification. The pre-push follow-up in §13 supersedes sandbox and
deployment-readiness claims where its stricter profile or database migration could not be verified.

* **ZIP scope:** original `QCR.zip` and `RFQ.zip` are absent. ZIP acceptance was recorded as `SKIPPED — OUT OF SCOPE`, not PASS;
  no archives were reconstructed. The hostile-archive browser case did run and pass.
* **macOS sandbox:** Seatbelt runtime confinement is verified. Office rendering and signature validation fail closed when their
  sandbox or required host dependency is unavailable; documents never run unconfined.
* **Linux:** Bubblewrap command construction has unit tests only. Runtime sandboxing is **not verified**, so Linux is not supported
  for deployment of these document workers until a confined runtime check passes.
* **Containers:** container runtime confinement is **not verified**. Do not treat the macOS or Bubblewrap command-construction
  results as container support.
* **Word fidelity:** LibreOffice is not Word. Fonts that are not installed are substituted and some effects can differ slightly;
  this is stated beside every page preview. Editing remains bounded to supported text (multi-run paragraphs, fields, text boxes
  stay read-only with an explanation); layout-changing edits are refused rather than approximated.
* **Supported environment:** verified macOS with Seatbelt, LibreOffice, OpenSSL 3+, PostgreSQL 16 and the configured scanner.
  Linux and container document processing are not verified or supported by this report.
* **Comparison limits (reported with every result):** PDF — visible text and position, not drawings/annotations; DOCX — text,
  tables, paragraph/run formatting and image content hashes, not image/shape position; XLSX — values, formulas, resolved
  formatting, merges, pictures and sizes, never recalculated values. The 180,830-cell benchmark compared completely; the browser
  keeps a bounded viewport and requests further windows as the user scrolls.
* **Excel:** formulas are shown and locked, never recalculated; pictures are preserved but not editable; `INCOMPLETE` derivatives
  with stale formula results remain withheld by the download gate.
* **Signatures:** trust requires an explicit CA file (`IDW_PDF_TRUST_CAFILE`); none is configured, so real signatures are
  "trust not established". Revocation (CRL/OCSP), long-term validation, timestamp-token validation and the legacy
  `adbe.pkcs7.sha1` container are not checked (a timestamp token's presence is reported, not validated). DocMDP is reported on a
  best-effort parse. Legal/regulatory acceptance is never claimed.
* **Working copies:** a stamped image or logo that was part of the page content stays visible (it is not a signature; the notice
  strip and metadata say so). Rotated pages show the strip rotated. Re-signing / newly-signed workflow is intentionally not
  implemented.
* **Chunks to page links** exist for DOCX sections whose heading text can be located unambiguously; others are not linked.
* **Shared application:** still runs the previous backend; this work did not restart or modify it. No deployment was performed.

## 12. Exact source files changed (since `1ba0813`)

* `apps/web/e2e/archive-import.spec.js`
* `apps/web/e2e/compare.spec.js`
* `apps/web/e2e/document-experience.spec.js`
* `apps/web/e2e/docx-pages.spec.js`
* `apps/web/e2e/global-setup.js`
* `apps/web/e2e/manager-ui.spec.js`
* `apps/web/e2e/perf-viewer.spec.js`
* `apps/web/e2e/repeat-docx-upload.spec.js`
* `apps/web/e2e/signed-pdf.spec.js`
* `apps/web/e2e/xlsx-workspace.spec.js`
* `apps/web/e2e/zz-demo-workspace-clean.spec.js`
* `apps/web/playwright.config.js`
* `apps/web/src/App.tsx`
* `apps/web/src/components/PageViewer.tsx`
* `apps/web/src/components/SheetWorkspace.tsx`
* `apps/web/src/components/SignaturePanel.tsx`
* `apps/web/src/lib/api.ts`
* `apps/web/src/lib/numfmt.ts`
* `apps/web/src/lib/textdiff.ts`
* `apps/web/src/pages/Compare.tsx`
* `apps/web/src/pages/editors.tsx`
* `apps/web/src/pages/Edits.tsx`
* `apps/web/src/pages/Library.tsx`
* `apps/web/src/pages/SheetGrid.tsx`
* `apps/web/src/pages/Shell.tsx`
* `apps/web/src/pages/Workspace.tsx`
* `apps/web/src/style.css`
* `docs/document-experience-upgrade.md`
* `packages/api/app.py`
* `packages/api/auth.py`
* `packages/api/compare.py`
* `packages/api/render.py`
* `packages/api/working_copy.py`
* `packages/api/workspace.py`
* `packages/core/adapters/docx/adapter.py`
* `packages/core/adapters/xlsx/adapter.py`
* `packages/core/compare.py`
* `packages/core/pdf_working_copy.py`
* `packages/core/xlsx_layout.py`
* `packages/registry/documents.py`
* `packages/workers/office_render.py`
* `packages/workers/pdf_edit_entry.py`
* `packages/workers/pdf_edit.py`
* `packages/workers/sigcheck.py`
* `scripts/corpus/acceptance_run.py`
* `scripts/corpus/chunk_quality.py`
* `scripts/corpus/compare_flow.py`
* `scripts/corpus/docx_api_flow.py`
* `scripts/corpus/docx_fidelity.py`
* `scripts/corpus/evidence_ledger.py`
* `scripts/corpus/final_audit_office.py`
* `scripts/corpus/final_audit_pdf.py`
* `scripts/corpus/final_audit_platform.py`
* `scripts/corpus/office_api_flow.py`
* `scripts/corpus/pdf_api_flow.py`
* `scripts/corpus/perf_check.py`
* `scripts/corpus/probe.py`
* `scripts/corpus/rebuild_archives.py`
* `scripts/corpus/security_flow.py`
* `scripts/corpus/signed_flow.py`
* `scripts/corpus/verify_office_edit.py`
* `scripts/corpus/xlsx_fidelity.py`
* `scripts/corpus/xlsx_benchmark.py` (inherited from Claude Code; finalized and benchmarked)
* `scripts/corpus/xlsx_index_fidelity.py` (inherited from Claude Code; rerun)
* `scripts/corpus/storage_read.py`
* `scripts/dev/iso_client.py`
* `scripts/dev/isolated-stack.sh`
* `scripts/dev/issue_iso_token.py`
* `scripts/dev/load_generator.py`
* `scripts/host/issue_test_token.py`
* `tests/m1/conftest.py`
* `tests/unit/test_compare_engine.py`
* `tests/unit/test_office_render.py`
* `tests/unit/test_pdf_working_copy.py`
* `tests/unit/test_sigcheck.py`
* `tests/unit/test_xlsx_transitive_dependencies.py`
* `tests/unit/test_corpus_runner_hardening.py`

## 13. Pre-push security findings: final verification (2026-11-14)

Four independent pre-push findings were re-examined on this macOS host (Darwin 25.6, arm64, PostgreSQL 16 on loopback, LibreOffice 26.8, OpenSSL 4.1) using only isolated resources (`channel360_iso_*` databases, `.runtime/iso-stack/storage.*`, ports 8010/5180). The shared backend `:8000`, frontend `:5174`, database `channel360_test` and shared document storage were not touched. Nothing was pushed or deployed.

| Finding | Final status | Evidence |
|---|---|---|
| F1 macOS sandbox | **FIXED and runtime-verified on macOS** | live Seatbelt canaries, DOCX/PDF/XLSX processing under confinement (section 13.1) |
| F2 isolated-database guard | **FIXED and verified** (two further defects found and fixed) | hostile-input matrix, real provisioning and drop (section 13.2) |
| F3 XLSX comparison memory | **FIXED and verified** | 180,000-cell stress with 174,150 changes (section 13.3) |
| F4 working-copy lineage | **FIXED and verified on real PostgreSQL** (the committed migration was broken; fixed) | 0007 -> 0008 execution, rollback, empty database, restart, tenant isolation (section 13.4) |

The environment restrictions Codex reported (`/bin/ps` denied, PostgreSQL cluster not initialising, sandbox worker not launching) are properties of that review environment: on this host `/bin/ps`, `sandbox-exec` and PostgreSQL work. The sandbox launch failure was nevertheless a **real application defect** (not only an environment restriction); the root causes are below.

### 13.1 F1 - macOS Seatbelt confinement

**Root causes of the launch failure** (found with minimal canaries, `/usr/bin/true` first, then the Python interpreter, then LibreOffice):

1. A deny-default profile cannot start any process unless the root directory entry `/` is readable (`(literal "/")`; only that one entry, not its children).
2. The interpreter/tool is reached through symlinks (virtualenv link, Homebrew `opt/` and `Cellar/` links, directory links). Every hop of the chain needs a literal read, an exec grant and metadata on its ancestors; the profile now resolves each hop (`SeatbeltRunner._chain`).
3. `/etc`, `/var`, `/tmp` are firmlinks; the link entries themselves (not their targets) must be readable.
4. LibreOffice additionally needs: the whole application bundle read-only (bundle root, not only `Contents`), Mach service lookups to start at all, local AF_UNIX sockets, and its single-instance IPC pipe `/private/var/tmp/OSL_PIPE_<uid>_SingleOfficeIPC_<hex>`. A blanket `(deny network*)` also broke it (launcher to `soffice.bin` socket); IP networking is still denied.
5. Test defect: the live canary test generated a child snippet with an unescaped newline (syntax error), so it never tested anything.

**What stays denied.** `(deny default)`; no `(allow default)`; writes only in the job scratch and `/dev/null`; **no** grant of `/private/var/tmp` as a directory tree, `/private/var/folders` or `/private/tmp`; the only write outside scratch is the exact OSL pipe name pattern plus the `/private/var/tmp` directory entry itself (children are not granted); IP networking denied; no `/Users`, credentials, project directory, shared storage or other jobs' scratch. Mach lookups (`allow mach*`) are granted to LibreOffice only, because it does not start without them; they carry no file access but are broader than ideal and remain a documented residual risk. The OSL pipe name hash could not be derived per job, so the pattern allows any `OSL_PIPE_<uid>_SingleOfficeIPC_*` name (a document cannot choose other file names).

**Runtime evidence (real enforcement, not profile inspection):**

* `GET /health/sandbox` and `scripts/dev/check-sandbox.py`: self-test PASSED (all forbidden actions refused, scratch writable), READY.
* `tests/unit/test_sandbox.py`: **16 passed, 0 skipped** including the two-job isolation test (own scratch read/write allowed; other job's file read and write denied; unrelated `/private/var/tmp` file read/write denied; credential file under home denied; shared-storage listing denied; project file denied; IP connection denied; child process confined) and a real LibreOffice render under live Seatbelt.
* Legitimate processing under confinement: DOCX rendering `docx_fidelity` **26/26**, `docx_geometry` **9/9**; PDF/signed `signed_flow` **82/82**; `security_flow` **23/23**; XLSX windowed index and save under the restricted worker `xlsx_benchmark` **32/32**, `xlsx_index_fidelity` **36/36**.

**Not verified (reported as such):** Linux Bubblewrap: **NOT RUNTIME VERIFIED** (command construction is unit-tested only). Container confinement: **NOT VERIFIED**. Other macOS versions: not tested.

### 13.2 F2 - isolated database and stack script

Defects found by actually running the script (the previous unit tests only exercised the validator module): (a) `set --` in the validation block replaced the positional parameters, so `up`, `down`, `status` and `drop` printed usage and did nothing; (b) `set -- $(validator) || ...` masks the validator's exit status, so invalid ports were not refused; (c) one storage directory was shared by every isolated database; storage is now per database (`storage.<name>`, the original name keeps `storage`); (d) `drop` refused a database provisioned with `db` (no storage directory yet).

Verified: refusal of `channel360_test`, `a;b`, an over-length name, ports 8000/5174/non-numeric/out-of-range, non-loopback PostgreSQL host, invalid port, malformed tenant UUID; ownership markers on database and storage; a disposable database (`channel360_iso_empty`) was provisioned, migrated and dropped; dropping `channel360_test` is refused. Pre-existing isolated databases were not deleted.

### 13.3 F3 - XLSX comparison memory (`scripts/corpus/xlsx_compare_stress.py`, fresh process per scan)

| Case | Cells scanned | Changes detected (independent oracle) | Returned | List status | Peak RSS / scan growth |
|---|---|---|---|---|---|
| small | 1,980 | 83 | 83 | COMPLETE | 78 MB / 1 MB |
| 60,000 cells | 60,830 | 4,101 | 4,101 | COMPLETE | 86 MB / 9 MB |
| 180,000 cells | 180,830 | 12,141 | 12,141 | COMPLETE | 92 MB / 15 MB |
| 180,000 cells, every value cell changed | 180,830 | **174,150** | 98,222 | **TRUNCATED** | 162 MB / 85 MB |

Counts equal an openpyxl cell-by-cell oracle in every case; no phantom additions or removals; formula edits (including the shared-formula column) counted; the tail edit far outside the first window is found. The scan stays COMPLETE while the retained list is capped at 16 MB / 200,000 records and flagged `list_status=TRUNCATED`, `has_more`, with `total_changes_detected` and `returned_changes`; the UI shows "Scan COMPLETE - list TRUNCATED - showing N of M". One defect fixed: the per-record `id` added at finalisation was not budgeted, so retained bytes could exceed the cap by about 12 percent. API-level comparison on the benchmark workbooks finds every edit (below).

### 13.4 F4 - migration 0008 on real PostgreSQL (`scripts/dev/lineage_migration_check.py`)

Executing the migration found that the committed version **did nothing** on a real database: every tenant table uses `FORCE ROW LEVEL SECURITY`, so the migration role saw zero rows, and not even `kind='original'` rows were classified. Fixed: the table owner lifts `FORCE` on `document_versions` and `audit_events` inside the migration transaction and restores it, and a post-condition raises if any original version is left unclassified. A second defect (a standard release whose parent was classified in the same `UPDATE` stayed `unknown`) is fixed by classifying with a recursive unknown-root query.

Verified on disposable databases: migrations 0001-0007 applied; historical data seeded (signed-PDF-style original, unsigned working copy, descendants, branching descendants, standard releases, legacy versions without provenance, a second tenant whose audit event names a first-tenant version); 0008 executed; column `text NOT NULL DEFAULT 'unknown'` and the CHECK constraint present and enforced; backfill **16/16** (original, working copies and all descendants and branches, standard, unknown roots and their descendants stay `unknown`, cross-tenant event ignored); 2,200 further audit events appended with classifications unchanged; the isolated API restarted with identical History labels (**14/14** API checks, tenant B receives 404 for tenant A's document); documented rollback (downgrade to 0007 removes column and constraint; re-upgrade reproduces the same classifications); an empty database migrates to 0008 with `FORCE ROW LEVEL SECURITY` intact. A missing stored object now yields a controlled 409 `stored_file_unavailable` instead of an unhandled 500 (found while exercising seeded rows without files).

### 13.5 Regression found by the full browser run

The bounded comparison cache introduced in the review commit stored results with `state.x = ...` but the cell-marks endpoint read `state.__dict__`; every marks request returned 409 and no changed cell was highlighted (4 Playwright specs failed). One shared accessor now serves both (`tests/unit/test_compare_cache.py`).

### 13.6 Final regression (fresh isolated PostgreSQL `channel360_iso_pgfinal`, production build on :5180)

| Suite | Result |
|---|---|
| Backend `pytest tests` (PostgreSQL URLs set) | **749 passed, 0 failed, 0 skipped** |
| Playwright, all specs except the ZIP spec | **88 passed, 0 failed** |
| Stakeholder acceptance, non-ZIP (`acceptance_run.py --skip-zip`) | **17 PASS, 0 FAIL**, ZIP stage reported OUT OF SCOPE |
| `compare_flow` / `branch_compare_flow` | 31/31, 11/11 |
| `xlsx_fidelity` / `xlsx_index_fidelity` | 91/91, 36/36 |
| `docx_fidelity` / `docx_geometry` | 26/26, 9/9 |
| `signed_flow` / `security_flow` | 82/82, 23/23 |
| `xlsx_benchmark` | 32/32 |
| `lineage_migration_check` / `xlsx_compare_stress` | all phases pass / 20/20 |

Workbook benchmark through the API (edit near the top and far down, native save, reopen, complete comparison):

| Workbook | Cells | Index ready | Window (max) | Save | Complete comparison |
|---|---|---|---|---|---|
| small RFQ | 604 | 0.23 s | 6 ms | 0.6 s | 0.24 s |
| medium QCR | 312 | 0.23 s | 6 ms | 0.7 s | 0.23 s |
| 60,000 cells | 60,830 | 1.7 s | 6 ms | 4.0 s | 1.8 s |
| 180,000 cells | 180,830 | 4.8 s | 6 ms | 11.3 s | 5.1 s |

### 13.7 Supported environments, dependencies, deployment, rollback

* **Verified:** macOS (Apple silicon, Darwin 25.6) with Seatbelt, PostgreSQL 16, LibreOffice 26.x, OpenSSL 3+ (4.1 tested). **Linux Bubblewrap: NOT RUNTIME VERIFIED. Container confinement: NOT VERIFIED.** Document previews and signature validation fail closed when the self-test does not pass; do not deploy to Linux or containers as production-ready until the Bubblewrap path has been run there.
* **Dependencies:** LibreOffice (with Carlito/Liberation/Caladea fonts, bundled with the macOS application), OpenSSL 3+, the host SQLite library for index reads, ClamAV socket for uploads.
* **Deployment:** back up the database; apply `alembic upgrade head` (0008 must run as the owner of `document_versions` and `audit_events`, because it temporarily lifts and restores `FORCE ROW LEVEL SECURITY`); then start the new API; check `GET /health/sandbox` reports `verified_here: true`. Derived caches `.preview-cache` and `.xlsx-index` are disposable.
* **Rollback:** stop the new API, `alembic downgrade 0007_edit_set_supersession` (drops the column and constraint; data and audit history are untouched), deploy the previous build. Re-upgrading reproduces identical classifications.

### 13.8 Remaining limitations

ZIP archive import is out of scope and not tested this cycle. The 180,000-cell first viewport is dominated by the one-time index build (about 5 s). Linux and container confinement are unverified. The LibreOffice Mach grant is broad. Marine PDF per-word text runs remain a known fidelity limit. Leftover isolated databases and storage directories are kept until cleanup is authorised.
