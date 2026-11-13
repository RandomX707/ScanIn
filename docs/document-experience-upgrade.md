# Channel360 — Document experience upgrade

Version comparison · Excel workspace · Word page rendering · Digitally signed PDF working copies

Status: implemented and verified on an **isolated stack** (own database, own storage, own tenants). The shared application on
`:8000` / `:5174` was not restarted or modified by this work. Nothing in this document contains stakeholder content, extracted
field values, document copies or secrets; private evidence stays under `.private/` (git-ignored).

Workflow now supported end to end:

```
UPLOAD → VIEW FAITHFULLY → UNDERSTAND → EDIT → PREVIEW → SAVE NATIVE VERSION → COMPARE ANY TWO VERSIONS → DOWNLOAD
```

## 1. Architecture

All four features extend the existing engine. There is no new trust path: every new capability that touches an uploaded file
runs in an OS-sandboxed worker (macOS Seatbelt; Linux Bubblewrap exists for the original parsers, see §8) and every new endpoint
is tenant-scoped through `api_tenant_transaction` (PostgreSQL row-level security).

| Area | New / changed code | Sandbox |
|---|---|---|
| Common viewer | `components/PageViewer.tsx` (PDF.js pages: zoom, fit width, actual size, page navigation, full screen, highlights) | browser |
| Comparison | `packages/core/compare.py` (pure diff engine), `packages/api/compare.py` (snapshots, lineage, audit, endpoints), `pages/Compare.tsx` | snapshots come from the existing sandboxed parsers/PDF worker |
| Excel | `components/SheetWorkspace.tsx`, `pages/SheetGrid.tsx`, `lib/numfmt.ts`, `packages/core/xlsx_layout.py` | layout extraction runs in the sandboxed parser |
| Word | `packages/workers/office_render.py`, `packages/api/render.py`, DOCX adapter (`fmt`, `props`, `media`) | LibreOffice under Seatbelt |
| Signed PDFs | `packages/workers/sigcheck.py`, `packages/core/pdf_working_copy.py`, `packages/api/working_copy.py`, `components/SignaturePanel.tsx` | OpenSSL under Seatbelt; working-copy builder in the PDF worker |

No database migration. The schema is unchanged (`0007_edit_set_supersession`). "Unsigned working copy" is recorded as an audit
payload flag (`unsigned_working_copy: true` on `document.text_version_saved`) and inherited down the parent chain.

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
* **Packages:** none new (`pypdf`, `reportlab`, `pdfjs-dist` already present). Host needs LibreOffice and OpenSSL 3+ on macOS.
* **Frontend:** served live by Vite on `:5174` (or `npm run build`); no new npm dependency.
* **Deploy:** restart only the backend with the same environment (back up the database and storage first); verify
  `/openapi.json` lists the new paths. Requires explicit approval; **not done by this work**.

## 6. Rollback

Code-only rollback; data is untouched by the new code (the only new stored artefacts are normal versions and the disposable
`.preview-cache`, which can be deleted at any time). Start the previous revision's code with the preserved environment file
(see the backup folder's `ROLLBACK.md` procedure). Working-copy versions already created remain valid PDF versions; an older
backend lists them as ordinary saved versions.

## 7. Verification results

Everything below ran against the **isolated stack** (database `channel360_iso_*`, own storage, fresh tenants per run) on the real
private stakeholder corpus. Counts only; no document content is reproduced here.

| Suite | Result |
|---|---|
| Backend `pytest tests` on a fresh isolated PostgreSQL database (migrator/app/worker roles, grants applied) | **690 passed, 0 failed, 0 skipped** (without database credentials: 658 passed, 32 skipped) |
| New backend unit tests (comparison engine 6, sandboxed Office render 5, real-CMS signature validation 8, working-copy builder 5) | 24 passed |
| Playwright, whole suite, production build of the frontend | **77 tests: 76 passed, 1 skipped, 0 failed**. The skip is the `QCR.zip`/`RFQ.zip` test (original archives absent, see §10); it passes (3/3 in its spec) against reconstructed archives |
| Stakeholder acceptance runner (17 documents + archive stage), 289 checks | **18 PASS, 0 SAFE RESTRICTION, 0 FAIL — 289/289**. Archive stage ran on *reconstructed* archives (§10) |
| Version comparison flow (PDF direct/cumulative/branch/reverse, XLSX incl. INCOMPLETE derivative, DOCX, authorization) | 31/31 |
| Signed-PDF flow (Health, Motor, Marine + unsigned control; status, acknowledgement, working copy, edit, lineage, audit, compare) | 82/82 |
| Security flow (5 new endpoints x unauthenticated / wrong role / other tenant; no path or tenant leakage; tampered and fabricated signatures; malformed DOCX; unsupported types) | 23/23 |
| XLSX layout vs an independent openpyxl parse of all 9 workbooks (merges, widths, heights, hidden rows/columns, frozen panes, pictures, protection, number formats) | 91/91 |
| DOCX fidelity and native edit/save/reopen on `Health Quote Format.docx` | 26/26 |
| Four-feature real-browser journeys (`document-experience.spec.js`) | 4/4 |
| Compare / signed-PDF / Excel-workspace (9 workbooks) / DOCX-pages / 72-page viewer browser specs | 3 / 2 / 9 / 1 / 1, all passing |
| Performance and stability (`perf_check.py`) | 16/16 |

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

Finding: the failure is reproduced by hot-reloading the dev server mid-test (page state is lost mid-flow), which matches what
was happening when it first failed (frontend sources were being edited while the suite ran). It is a test-environment
interference, not a product defect, but the original instance cannot be proven to be this cause because its artefacts were
overwritten. Mitigation: `C360_ISO_WEB_MODE=preview scripts/dev/isolated-stack.sh up` serves a production build; all final
browser runs used it.

## 8. Measured fidelity

**Word (`Health Quote Format.docx`, LibreOffice preview vs the file's own XML):** page size error 0.01 pt (A4); text coverage
100 %; hyperlinks 12/12 present as real links; embedded images 2/2; logo block spans 52 % of the page width; illustration paints
12.3 % of its band; the DOCX-defined heading colour is rendered (3.1 % of pixels); bullets and the comparison table render;
4 pages. **The file defines no page background (no `w:background`), so the faithful rendering is a white page; a dark canvas
seen elsewhere comes from the viewer's own theme, not from the file.** Native edit: exactly one paragraph differs, media/styles/
numbering/theme/settings/headers/footers are byte-identical, the saved file re-renders to the same 4 pages, and the change touches
0.05 % of the pixels of one page.

**Excel (9 workbooks):** layout model equals an independent parse for sheet names, merged ranges, column widths, row heights,
hidden rows/columns/sheets, frozen panes, picture anchors, sheet protection and number formats (91/91). The browser spec
verifies, per workbook: headers and formula bar, every formula shown and locked, zoom proportional (90 % = 0.90 of 100 %), fit
width, sticky headers while scrolling, full screen covering the viewport with the selected cell preserved, unsaved edits
surviving expand/collapse, native save. Native edits reopen independently with only the edited cell changed.

**Signed PDFs:** Health and Motor: signature intact, signer **not trusted** (no trust policy configured). Marine: **malformed**
(byte range runs past the end of the file). A one-byte change inside a signed range reports "does not verify"; a fabricated
signature object is reported malformed; none is ever reported trusted. Working copies of all three are unsigned (no signature
structure of any kind), carry the notice on every page, keep original page text, and the signed originals stay byte-identical.

## 9. Performance (isolated stack, idle machine)

| Case | Result |
|---|---|
| 72-page Fire PDF: import+inspect / preview / sections (cold) | 0.39 s / 0.32 s / 4.5 s |
| 72-page PDF: verified edit+save / compare cold / compare repeat (cache) | 2.4 s / 6.9 s / 0.03 s |
| 72-page viewer in the browser: first page painted / scroll through all 72 pages / console errors | 7.9 s (includes the cold compare) / 4.3 s / 0 |
| History with signature status of every version | 0.01 s |
| DOCX render: cold / cached / 6 parallel requests | 1.3 s / 0.005 s / 0.97 s total |
| Synthetic 180,000-cell workbook: import / preview / native save | 4.0 s / 25.6 s (flagged *partial*) / 34 s |
| Same workbook: compare cold / repeat | 90 s (first 16,000 cells, flagged incomplete) / 0.03 s |
| API process memory after the run | 80 → 102 MB |

Pre-optimisation baseline (kept in `.private/feature-results/perf-baseline-prefix.json`): workbooks of 60,000 and 180,000 cells
returned `503` after the 30 s worker timeout and a 15,000-cell workbook took 7.4 s to import. Two defects in the XLSX adapter
caused this (per-cell re-scan of data validations and per-cell SHA-256 of the whole file); fixing them made inspection linear
(180,000 cells: 38.8 s → 2.2 s in the adapter). A pre-fix benchmark that was still running was terminated.

## 10. Failures found during verification, and how each was resolved

| # | Finding | Resolution |
|---|---|---|
| 1 | XLSX inspection was quadratic; 60,000+ cells timed out (`503`) | Hoisted per-sheet work out of the per-cell loop (behaviour unchanged, all XLSX tests and acceptance pass) |
| 2 | Comparison of a very large workbook reported 2,999 phantom "added" cells (each side was cut at a different point by a time budget) | Both sides now read the same fixed window (first 16,000 cells), flagged `incomplete`, limit stated; verified: exactly one real change |
| 3 | Large-workbook preview took 77 s | Time-boxed to 25 s and flagged `partial` in the API and UI |
| 4 | Acceptance runner's audit-chain check connected to the **shared** `channel360_test` database (read-only, by a hard-coded URL) | Now reads only the isolated database; refuses any other name. One read-only connection attempt to the shared database occurred before this was caught; nothing was written |
| 5 | My signed-PDF flow silently skipped the Marine edit check (that PDF stores one word per text object) | Missing editable text is now an explicit failing check; Marine edits and saves; 82/82 |
| 6 | A browser test failed once | Cause reproduced and mitigated (§7.1) |
| 7 | Several test-script defects (wrong selector scope, whitespace-normalised text, caret placement in a wrapped paragraph, page-1-only line search) | Fixed in the tests; no product behaviour was changed to make a test pass |
| 8 | An earlier `APIError(details=…)` call passed `details` positionally (500 instead of 422) in two new endpoints | Fixed; covered by tests |

## 11. Known limitations and unresolved blockers

* **BLOCKED — original archives:** `QCR.zip` and `RFQ.zip` are no longer in `Policy Copies/` (only extracted folders). The archive
  stage and its browser test were therefore run on archives **reconstructed** from the retained extraction with identical entry
  names, paths and file bytes (`scripts/corpus/rebuild_archives.py`, `CHANNEL360_ZIP_DIR`). They are labelled RECONSTRUCTED in the
  results. Re-run with the original archives when they are available.
* **Platform:** the new sandboxed workers (Office render, signature validation) are implemented for macOS Seatbelt only. On
  hosts without it they report *unavailable* and the UI falls back to labelled extracted text / "not validated"; they never run
  unconfined. Linux Bubblewrap profiles for these two workers are not written.
* **Word fidelity:** LibreOffice is not Word. Fonts that are not installed are substituted and some effects can differ slightly;
  this is stated beside every page preview. Editing remains bounded to supported text (multi-run paragraphs, fields, text boxes
  stay read-only with an explanation); layout-changing edits are refused rather than approximated.
* **Comparison limits (reported with every result):** PDF — visible text and its position only (not drawings/annotations);
  DOCX — text, tables, paragraph/run formatting and image content hashes, not image/shape position; XLSX — values, formulas,
  resolved formatting, merges, pictures and sizes, never recalculated values; very large workbooks — first 16,000 cells.
* **Large workbooks:** a 180,000-cell sheet previews partially (25 s), saves in ~34 s and compares in ~90 s; the worker re-reads the
  whole workbook for every 2,000-target page. A windowed XLSX adapter would remove this and was not attempted because it would
  touch the verified ordering/fingerprint logic.
* **Excel:** display-window cap of 500 rows x 40 columns (flagged); formulas are shown, never recalculated; pictures are
  preserved but not editable; `INCOMPLETE` (stale formula results) derivatives keep their download gate.
* **Signatures:** trust requires an explicit CA file (`IDW_PDF_TRUST_CAFILE`); none is configured, so real signatures are
  "trust not established". Revocation (CRL/OCSP), long-term validation, timestamp-token validation and the legacy
  `adbe.pkcs7.sha1` container are not checked (a timestamp token's presence is reported, not validated). DocMDP is reported on a
  best-effort parse. Legal/regulatory acceptance is never claimed.
* **Working copies:** a stamped image or logo that was part of the page content stays visible (it is not a signature; the notice
  strip and metadata say so). Rotated pages show the strip rotated. Re-signing / newly-signed workflow is intentionally not
  implemented.
* **Chunks to page links** exist for DOCX sections whose heading text can be located unambiguously; others are not linked.
* **Shared application:** still runs the previous backend; deployment of this work needs explicit approval (§5).

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
