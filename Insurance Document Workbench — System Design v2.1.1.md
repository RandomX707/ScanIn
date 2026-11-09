# Insurance Document Workbench — System Design v2.1.1

Nov 6, 2026 · @yashaswi ram

The source of truth for implementation: a deterministic core that edits files in their own format and proves nothing unauthorised changed, with intelligence added later as optional plugins. Supersedes Doc.

## Current product decision — single-user owner workflow (Nov 9, 2026)

This decision supersedes the multi-role and human-approval assumptions retained in the historical design sections below. Channel360 is a single-user document workbench: one authenticated `OWNER` operates their own tenant-scoped workflow. The owner may inspect, classify, correct, validate, request automated verification, release and download their documents. The application has no processor/reviewer/administrator switching, approval queue, approve/reject action or human approval requirement. No reviewer identity or approval event is fabricated.

The active edit-set state path is `draft → ready_for_release → released` (or `failed`). A ready edit set is not yet released. The owner explicitly requests release; the server revalidates the frozen plan, current source, category policy, target fingerprints and expected values, then applies the patch, runs independent format-specific verification and malware scanning. Only outcomes authorized for that adapter are committed and downloadable. Each successful release records an append-only `document.release_authorized_by_owner` event with the authenticated owner and lineage. Original bytes remain immutable, RLS and tenant checks remain mandatory, and category, signature, format and target restrictions are unchanged.

The database retains legacy role values, review records and submitted/approved/rejected edit-set states to preserve history. Those states are not part of the active workflow, cannot authorize a new release, and are never silently migrated into approval. Existing pending legacy work must be reopened as a new draft and preflighted against the current source.

DOCX's default `structural` profile permits release as `STRUCTURALLY_VERIFIED` only after all configured structural/content checks pass; its visual status is `NOT_PERFORMED`. The optional `structural_and_visual` profile still requires visual checks and fails closed if its renderer is unavailable. Structural verification does not promise identical pagination, wrapping or appearance.

## Settled decisions

These decisions are closed; changing one needs an explicit revision of this document.

| Area | Decision |
| --- | --- |
| Scope | Any file enters upload and assessment; each file gets an explicit capability level |
| Core | Deterministic; upload, view, select, edit, verify and download all work with no LLM |
| Corrections | Two kinds: a data correction changes the workbench value only; a document correction changes a new copy of the file |
| Model | Versions, evidence, edit targets, fields, patches with preconditions, change manifests, each owned by one component |
| Field discovery | Manual selection and deterministic templates in v1; providers are pluggable |
| Write-back | Patch a copy of a named source version; never regenerate the file |
| Verification | Layered, independent and fail-closed; each check returns PASS, FAIL or NOT\_VERIFIED |
| Authority | Configurable permissions on top of fixed invariants that no configuration can switch off |
| Architecture | Modular monolith: React, FastAPI, Redis-backed workers, Postgres, object storage |
| PDF stack | pypdf for forms, pypdfium2 for rendering; no PyMuPDF |
| Parsing | Native parsers per format; Docling only as an optional plugin |
| Security | Tenant scoping, roles, sandboxed workers and audit from the first milestone |
| Delivery | Milestones close when their acceptance tests pass, not on dates |

## Architecture

A modular monolith in which only the core engine can produce a patch, and only the verifier can release a copy. Plugins sit beside the core and can do nothing but propose fields.

&#91;embedded content: architecture v2.1 · core engine, optional plugins, independent verification\]

In code these layers are Python packages in one repository. At runtime they are three worker profiles from one image: ingestion (detect, scan, parse, preview), intelligence (templates, rules, optional plugins, network only to allowed providers) and export (apply and verify, no network). Each profile has its own resource limits.

## Capability profiles and authority

What a user may do with a file is decided by three things in order: what the file allows, what the target inside it allows, and what policy allows. Fixed invariants sit under all three.

**Capability levels.** The adapter's `inspect` assigns one level per file, with a reason shown to the user. The level is computed per file, not per format: a plain XLSX is Editable, an XLSM with macros is Read-only.

| Level | What the user gets | Examples |
| --- | --- | --- |
| Editable | View, fields, data corrections, document corrections | CSV, JSON, XLSX without macros, unsigned fillable PDF, DOCX (single-run targets only) |
| Read-only | View, fields, data corrections; corrections export as a sidecar file | Digital and scanned PDFs, images, signed or certified PDFs, XFA forms, DOCX with tracked changes or protection, XLSM, legacy Office, email |
| Limited | Metadata and whatever text can be read safely | Unknown formats with readable text, archives with unsupported members |
| Rejected | File quarantined, nothing processed, reason shown | Encrypted without a password, executables, malware matches, corrupted files, files over limits |

**Target editability.** Inside an Editable file, each edit target carries `editable` and a `reason`. Examples: a formula cell (`formula_cell`), a DOCX span across runs (`multi_run_span`), a read-only PDF field (`readonly_field`).

**Document category.** Two separate values, so classification can never unlock editing by accident.

- `suggested_category` may come from the uploader, a matched template or a plugin. It grants nothing.
- `authorized_category` is set by the authenticated owner through the audited classification action, or by a separately configured trusted ingestion source. Classification grants no ability to bypass format, signature, protection or target-level restrictions.
- Policy reads only `authorized_category`. Until it is set, the document is treated as unknown, which allows data corrections only.
- Categories: insurer-authored, customer form, third-party issued, signed or legal, unknown. The default policy allows document corrections only on documents authorised as insurer-authored.

**Authority policy.**

| Configurable per tenant | Fixed in code |
| --- | --- |
| Which roles may make document corrections, per category | The original is always preserved, byte for byte |
| Which fields may be edited | A copy is labelled verified only when every required check passes |
| Human approval required for a change | Every action writes an audit record |
| Whether copies carry a visible edited stamp | A signed or certified document is never modified as if its signature still held |
| Which plugins may receive document text | Files change only through patches with preconditions, never through plugins |

## Canonical model

Each concept has exactly one owner, and no other component may set its values. This is what stops a template, a plugin or the UI from deciding which bytes get overwritten.

| Concept | Answers | Owner |
| --- | --- | --- |
| Source version | Which exact file bytes are we talking about? | Storage and versioning |
| Evidence | Where was this value seen? | Parser |
| Edit target | Where can a value be changed safely, and is it editable? | Adapter |
| Field | What does this value mean? | Field discovery (manual, template, plugin) |
| Permission | Is this actor allowed to change it? | Policy engine |
| Data correction | What should the workbench record instead? | Authenticated owner |
| Patch | What exact change goes into a new copy? | Authenticated owner, checked by the adapter and independent verifier |
| Change manifest | What changed, what was allowed to change, and what was verified? | Verifier |

```python
class TypedValue(BaseModel):         # values are never compared as raw strings
    type: Literal["string", "integer", "decimal", "date", "datetime", "boolean", "choice", "null"]
    value: str | int | Decimal | date | datetime | bool | None
    native: str | None               # as stored in the file: "46310" (Excel serial), "15/10/2026"
    display: str | None              # as the file displays it, when the adapter can compute it

class SourceVersion(BaseModel):
    id: str                          # "ver_01"
    document_id: str
    parent_version_id: str | None    # None for the original upload
    kind: Literal["original", "derived"]
    file_id: str
    sha256: str
    edit_set_id: str | None          # the edit set that produced a derived version

class Evidence(BaseModel):           # where a value was observed
    version_id: str
    block_id: str
    page: int | None
    bbox: tuple[float, float, float, float] | None
    span: tuple[int, int] | None     # byte offsets for text formats
    text: str

class EditTarget(BaseModel):         # where a value can be changed
    id: str                          # "xlsx:Claim!D12"
    version_id: str
    kind: Literal["csv_cell", "json_pointer", "xlsx_cell", "pdf_form_field", "docx_run_span"]
    locator: dict                    # format-native address
    current_value: TypedValue
    fingerprint: str                 # hash of the target's native bytes or canonical XML node
    editable: bool
    reason: str | None               # why not editable
    dependents: list[str] | None     # None means unknowable, treated as having dependents

class Field(BaseModel):              # what a value means
    id: str
    key: str | None                  # "invoice_total"; None for an unnamed manual field
    label: str
    source: Literal["manual", "template", "plugin"]
    evidence: list[Evidence]
    target_id: str | None            # None when no safe edit target exists
    observed_value: TypedValue
    signals: dict                    # ocr_confidence, grounded, normalised, rule results
    review_status: Literal["unreviewed", "accepted", "corrected"]

class DataCorrection(BaseModel):     # file untouched
    field_id: str
    corrected_value: TypedValue
    reason: str
    actor_id: str

class Patch(BaseModel):              # goes into a new copy
    source_version_id: str
    target_id: str
    expected_old_value: TypedValue
    expected_fingerprint: str        # the target's fingerprint when the patch was drafted
    new_value: TypedValue
    review_id: str | None

class ChangePermission(BaseModel):   # machine-enforced boundary for the verifier
    part: str                        # "xl/worksheets/sheet1.xml", "docProps/custom.xml", "body" for CSV and JSON
    locator: str | None              # "D12", "/claim/total"; None for the whole part
    operation: Literal["replace_value", "set_attribute", "add_part", "replace_part", "append_increment"]
    old_fingerprint: str | None      # None when the part did not exist
    expected_new_fingerprint: str    # what the verifier must find afterwards

class ChangeManifest(BaseModel):
    source_version_id: str
    source_sha256: str
    output_sha256: str
    patches: list[Patch]
    permitted_changes: list[ChangePermission]
    checks: list[CheckResult]
    outcome: Literal["VERIFIED", "INCOMPLETE", "BLOCKED"]
    adapter: str
    adapter_version: str
    actor_id: str
    approved_by: str | None
    created_at: datetime
```

**Data correction vs document correction.** A data correction says the workbench should record a different value, for example because the extraction was wrong or the insurer accepts a different figure. It never touches the file, is always available, and can be exported as a sidecar. A document correction changes the value inside a new copy of the file. It needs an editable target, an editable file and permission. The UI offers them as two separately labelled actions on every field.

**Supporting types.** The contracts the adapters and the export job exchange:

```python
class Block(BaseModel):
    id: str
    version_id: str
    kind: Literal["paragraph", "line", "table_cell", "cell", "form_field", "value", "header"]
    page: int | None
    text: str
    bbox: tuple[float, float, float, float] | None
    locator: dict

class ParsedDocument(BaseModel):
    version_id: str
    blocks: list[Block]
    children: list["ParsedDocument"]  # email attachments, archive members
    warnings: list[str]

class CapabilityProfile(BaseModel):
    level: Literal["editable", "read_only", "limited", "rejected"]
    reasons: list[str]               # "macros_present", "signed", "adapter_not_implemented"
    features: list[str]              # "shared_strings", "formulas", "charts", "external_links"
    adapter: str
    adapter_version: str

class Selection(BaseModel):
    version_id: str
    kind: Literal["cell", "json_pointer", "pdf_field", "text_span", "box"]
    locator: dict                    # {"sheet": "Claim", "cell": "D12"} or {"block_id": "b42", "start": 7, "end": 13}

class PatchCheck(BaseModel):
    target_id: str
    ok: bool
    reason: str | None               # "expected_value_mismatch", "fingerprint_mismatch", "too_wide", "unsafe_csv_value"

class PreflightReport(BaseModel):
    ok: bool
    patches: list[PatchCheck]
    permitted_changes: list[ChangePermission]

class CheckResult(BaseModel):
    check: Literal["integrity", "intent", "preservation", "visual", "dependents", "policy", "provenance"]
    status: Literal["PASS", "FAIL", "NOT_VERIFIED"]
    critical: bool
    detail: dict

class VerifyReport(BaseModel):
    checks: list[CheckResult]
    outcome: Literal["VERIFIED", "INCOMPLETE", "BLOCKED"]

class Provenance(BaseModel):
    source_sha256: str
    edit_set_id: str
    tool_version: str
    created_at: datetime

ExportJobState = Literal["queued", "preflighting", "applying", "staged", "verifying",
                         "committing", "released", "blocked", "failed"]
```

Comparisons use `TypedValue.value`, so a workbook date serial, a displayed date and an ISO date for the same day compare equal. `native` and `display` are checked separately by the intent check.

## Field discovery

Fields come from three providers behind one interface. In v1, manual selection works on any viewable file, and templates work on known layouts.

**Manual mode.** The user selects a cell, a form field, a JSON value or a text span in the viewer. The adapter's `target_at(selection)` resolves it to an edit target, or returns none with a reason. Either way a field is created. Without a target, it supports data corrections only. This mode needs no template and no AI, so native write-back can be built and tested first.

**Template mode.** A template is a versioned YAML file. It matches a known layout deterministically and maps locations to named, typed fields with rules. The user confirms the match the first time a template is applied to a document.

```yaml
id: motor-claim-worksheet
version: 1
match:
  format: xlsx
  sheet_names: [Claim]
  cells: {A1: "Motor Claim Worksheet"}
suggested_category: insurer_authored   # a suggestion only; never grants editing
fields:
  - key: claim_number
    label: Claim number
    locator: {sheet: Claim, cell: C3}
    type: string
    rules: [required, {pattern: "^CLM-\\d{4}-\\d{5}$"}]
  - key: loss_date
    label: Date of loss
    locator: {sheet: Claim, cell: C6}
    type: date
    rules: [required, {between_fields: [policy_start, policy_end]}]
  - key: invoice_total
    label: Invoice total
    locator: {sheet: Claim, cell: D12}
    type: money
    rules: [required, {equals_sum: "D5:D11"}]
```

Locators per format: a cell or column for spreadsheets and CSV, a JSON pointer, a PDF form field name, and for text documents a label rule (the value after a given label, inside a given paragraph or table cell).

**Rules engine (planned).** Rules produce findings, never edits. Planned rule types include `required`, `pattern`, `range`, `between_fields`, `equals_sum`, `one_of`, and `matches_field` for comparing documents within a case. A finding records whether a value may be wrong, disagrees with another document, or a field may be located incorrectly. The authenticated owner chooses whether to record a data correction or create a native correction draft.

**Plugin interfaces.** Defined now, implemented with deterministic providers in v1:

```python
class FieldDiscoveryProvider(Protocol):
    name: str
    version: str
    def discover(self, doc: ParsedDocument, template: Template | None) -> list[FieldProposal]: ...

class SummaryProvider(Protocol):
    name: str
    version: str
    def summarize(self, doc: ParsedDocument, fields: list[Field], findings: list[Finding]) -> Summary: ...
```

v1 providers are `ManualProvider`, `TemplateProvider` and `StructuralSummary`. The structural summary reports document type, capability level, page, sheet or record counts, fields found, rule findings and version history. Later LLM providers return `FieldProposal`s only. Each proposal must cite evidence that the core grounds in the parsed text before it becomes a field, and proposals never carry edit targets.

## Adapter contract and registry

Every format implements one interface. An adapter that cannot verify its own output may not offer document corrections.

```python
class DocumentAdapter(Protocol):
    name: str
    version: str

    def sniff(self, head: bytes, filename: str) -> float: ...                  # 0..1, content first
    def inspect(self, source: bytes) -> CapabilityProfile: ...                 # level, reasons, features found
    def parse(self, source: bytes) -> ParsedDocument: ...                      # blocks and evidence
    def targets(self, source: bytes) -> list[EditTarget]: ...
    def target_at(self, source: bytes, selection: Selection) -> EditTarget | None: ...
    def render_preview(self, source: bytes) -> list[PreviewAsset]: ...
    def preflight(self, source: bytes, patches: list[Patch]) -> PreflightReport: ...
    def permitted_changes(self, source: bytes, patches: list[Patch]) -> list[ChangePermission]: ...
    def apply(self, source: bytes, patches: list[Patch], provenance: Provenance) -> bytes: ...
    def verify(self, source: bytes, output: bytes, patches: list[Patch]) -> VerifyReport: ...
```

Rules every adapter must follow:

- `apply` is a pure function: the same source, patches and provenance always give the same output bytes, and the source is never mutated.
- `preflight` rejects anything it cannot guarantee. It never silently adjusts a value to make it fit.
- `verify` reads the output through a different code path from `apply` where one exists. Example: XLSX is written with lxml and read back with openpyxl.
- `verify` declares which checks it can run. A required check it cannot run returns NOT\_VERIFIED, never PASS.
- Every result records the adapter name and version, so any copy can be reproduced.

**No-op rule.** An edit set with no patches, or whose every patch sets a value equal to the current one, never reaches `apply`. The export job returns the base version unchanged, creates no derived version and embeds no provenance, and records the attempt in the audit log.

**Registry.** Every adapter sniffs the first 8 KB plus the filename; content outweighs extension, and a mismatch between the two is itself a finding. The highest score wins, and ties go to the stricter adapter. ZIP-based files are opened and their content types read before an Office adapter is chosen. No score above 0.5 routes to the Limited adapter.

## Adapter specifications for v1

Five editable adapters ship in v1. Each one states what it rejects as clearly as what it supports, because the rejections are what keep the verified label honest.

### CSV

- **Inspect:** detect encoding (including a UTF-8 BOM and Windows-1252), delimiter, quote character and line endings.
- **Targets:** one per cell, addressed by row and column. A header row gives column names.
- **Unsafe values:** a new value beginning with `=`, `+`, `-`, `@`, a tab or a carriage return can run as a formula when the file is opened in a spreadsheet. Preflight rejects it with `unsafe_csv_value`. The exception is a value typed as integer or decimal that parses as a plain number, such as -500. A tenant may allow such values per field, with the export disclosing it. The value is never silently prefixed or altered.
- **Write:** splice only the target cell's bytes. Re-quote only if the new value needs it. Keep the encoding, delimiter and line endings.
- **Permitted changes:** the bytes of the target cells.
- **Verify:** re-parse; every other cell is equal; every byte outside the edited cells is identical.

### JSON

- **Inspect:** the file must be UTF-8, as RFC 8259 requires for interchange; any other encoding makes it Read-only.
- **Targets:** one per scalar value, addressed by JSON pointer. Targets inside an object with duplicate member names are not editable (`duplicate_keys`), because parsers disagree on which duplicate a pointer means.
- **Write:** a position-tracking tokenizer gives byte-accurate token spans. The new value is encoded as a JSON token, using the same style the file already uses for non-ASCII characters (literal UTF-8 or `\u` escapes), and spliced into the original bytes. The document is never re-serialised. A number stays a number and a string stays a string unless the patch states a type change.
- **Permitted changes:** the target token spans.
- **Verify:** parse both files; deep-equal except at the target pointers; bytes outside the spans are identical.

### XLSX

- **Inspect:** XLSM or a macro project makes the file Read-only. Encryption makes it Rejected. Shared strings, formulas, charts, images, external links and pivot caches are recorded as features.
- **Targets:** cells holding constant values. Not editable in v1: formula cells (`formula_cell`), rich-text cells with formatted runs (`rich_text`), and cells inside array or dynamic-array formula ranges (`array_formula`). Each target lists its formula dependents, or `None` when they cannot be known.
- **Write:** patch the target `<c>` element in the sheet XML with lxml.
  - Strings are written as inline strings (`t="inlineStr"`), so `sharedStrings.xml` is never touched and no other cell can change. Leading and trailing whitespace is kept with `xml:space="preserve"`.
  - Numbers drop the type attribute and write `<v>`. A value needing more than 15 significant digits is rejected, since Excel cannot store it exactly.
  - Dates are converted to serial numbers using the workbook's date system (1900 or 1904, read from `workbook.xml`).
  - Booleans use `t="b"`.
  - The style attribute `s` is always kept, so number and date formats stay.
- **Formulas and recalculation:** dependency analysis supports a defined subset: A1-style cell and range references, cross-sheet references, and defined names that resolve to ranges. Anything else makes dependents unknowable: `INDIRECT`, `OFFSET`, structured table references, external links, array formulas, R1C1 notation.
  - If dependents exist or are unknowable, the adapter sets `fullCalcOnLoad="1"` on `calcPr`. The dependents check returns NOT\_VERIFIED, so the outcome is at best INCOMPLETE, never VERIFIED. The export lists the affected cells.
  - LibreOffice headless may recalculate a scratch copy to show the expected new results. That copy is never the output.
- **Permitted changes:** the target `<c>` elements, `calcPr` in `workbook.xml` when needed, and the provenance parts (see OOXML packaging).
- **Verify:** unlisted parts have identical uncompressed contents. Canonicalised (C14N) sheet XML differs only at target cells. openpyxl reads back each target's value and type. The displayed value is computed from the cell's number format and compared with the expected display, for dates, currency, percentages and decimals. Styles, relationships and content types are unchanged. LibreOffice opens the file.
- **Excel qualification:** if Excel compatibility is a product requirement, the XLSX fixtures are also opened in Microsoft Excel before each release. LibreOffice compatibility alone does not prove Excel behaviour.

### Fillable PDF

- **Inspect:** signature fields that are signed, or DocMDP certification, make the file Read-only. So do XFA forms. Encrypted with a user password makes it Rejected.
- **Targets:** text, checkbox, radio and choice fields. Fields flagged read-only, or with calculation actions, are not editable.
- **Preflight:** the value respects `MaxLen`, is a valid option for choice and radio fields, and fits the widget width at the field's font size; otherwise the patch is rejected.
- **Write:** pypdf sets field values and regenerates appearance streams. Use pypdf's incremental writing mode, so the original bytes stay an unchanged prefix of the output. Before building the full adapter, prove value, appearance stream, font and widget rendering on fixtures.
- **Permitted changes:** the target fields' value and appearance objects, plus the provenance metadata.
- **Verify:** re-read every field; only targets changed. Page count, other annotations and page content streams are unchanged. pypdfium2 renders every page before and after, and pixels outside target widgets must match. qpdf `--check` covers syntax only.

### DOCX

**Product verification decision:** Standard supported DOCX corrections require structural and content verification. Visual layout verification is an optional stricter profile, not a mandatory requirement for ordinary structural-only releases.

- **Inspect:** tracked changes, document protection or a macro project make the file Read-only.
- **Targets:** text spans that lie entirely inside a single `w:t` within one `w:r`, in the body, tables, headers and footers. v1 excludes spans in field codes, hyperlinks, content controls and text boxes, and any span crossing runs (`multi_run_span`).
- **Write:** replace the substring inside that `w:t`. Set `xml:space="preserve"` when the new value has leading or trailing spaces. Run properties are never touched.
- **Permitted changes:** the target `w:t` elements and the provenance parts.
- **Structural verification profile (default):** part-level comparison as for XLSX. C14N of supported target parts differs only at intended text nodes; unlisted parts, styles, relationships, media, member order and compression methods remain unchanged; provenance, source hash, target fingerprints, expected old text, exact new text and package readability are checked. A pass is `STRUCTURALLY_VERIFIED`. LibreOffice, PDF conversion, pixel comparison and layout geometry are not run.
- **Strict visual profile (optional):** `structural_and_visual` runs every structural check plus the independent before/after render, page-count, line-wrap, glyph-position and outside-target pixel checks. If the isolated renderer or any critical visual check is unavailable or fails, the result is `BLOCKED`; the server never downgrades this profile.
- **Layout limitation:** structural verification does not guarantee pagination, line wrapping, font rendering or appearance across Word processors. Longer text may reflow. Only the strict profile evaluates rendered layout.
- **Later:** multi-run spans only after a dedicated test corpus exists.

### OOXML packaging (XLSX and DOCX)

Repack the ZIP with the original member order, names and per-member compression method. Fidelity is judged on uncompressed part contents, not container bytes. Provenance is written to `docProps/custom.xml`. If that part did not exist, adding it also changes `[Content_Types].xml` and `_rels/.rels`, and all three are listed as permitted changes.

### Read-only adapters in v1

| Adapter | View | Evidence for manual fields |
| --- | --- | --- |
| Digital PDF | pdf.js | pypdfium2 text with positions |
| Scanned PDF and images | pdf.js or image canvas | None without the OCR plugin; view only |
| Email (EML, MSG) | Sandboxed body plus attachment list; attachments go through their own adapters | Body text spans |
| Legacy Office (DOC, XLS, RTF) | LibreOffice-converted preview | Text from the converted copy, marked as converted |
| PPTX | LibreOffice-converted preview | Shape text |
| Limited | Metadata and extracted text | Text spans where available |

## Write-back and verification

A document correction becomes a downloadable copy only after policy, preflight and an independent verifier agree. Data corrections skip the file path entirely.

1. **Draft.** Patches collect in a server-side draft edit set bound to a base version. Drafts are authenticated, encrypted at rest, and never kept in browser storage.
2. **Submit.** The edit set is frozen and hashed. The idempotency key for its export is the edit set id plus that hash.
3. **Owner authorization.** The API checks the authenticated OWNER, tenant ownership, `authorized_category`, field permissions and the frozen plan. There is no human approval step. The owner explicitly requests release after preflight; a changed source or plan fails closed.
4. **No-op check.** If nothing would change, the base version is returned (see the no-op rule).
5. **Preflight.** The adapter confirms, for each patch:
   - the target exists in the base version;
   - `expected_old_value` and `expected_fingerprint` still match;
   - the new value is valid for its type, safe, and fits;
   - no blocked dependents exist.

   Any failure stops the edit set with reasons.
6. **Stage.** The export worker writes the candidate once to its content-addressed key under `derived/`, a private prefix. An object becomes downloadable only when a committed version row references it.
7. **Verify.** A separate worker process runs the checks below and computes the outcome.
8. **Commit.** In one Postgres transaction:
   - lock the edit set row (`SELECT … FOR UPDATE`) and confirm it is still `verifying` with the same hash;
   - insert the derived version, manifest, verification results and audit events;
   - run `UPDATE documents SET current_version_id = :new WHERE id = :doc AND current_version_id = :base`;
   - if that updates no rows, another release won: the new version is recorded as a branch, not as current, and the user is told.

   Only then is the edit set marked released.
9. **Retry and clean-up.** A retry with the same idempotency key finds the committed version and returns it, so no duplicate is created. A crash before commit leaves only an unreferenced object. A sweeper deletes unreferenced objects under `derived/` after 24 hours.

| Check | Question it answers | Typical method |
| --- | --- | --- |
| Integrity | Does the file open and parse? | Re-parse; zip and XML validity; qpdf syntax check; LibreOffice open |
| Intent | Does every target now hold its new value and type? | Read back each target through a separate library |
| Preservation | Did anything outside the permitted changes change? | Part-level and C14N comparison; byte comparison for text formats; field-by-field for PDF forms |
| Visual | Does the rendering match outside edited regions? | pypdfium2 or LibreOffice render, pixel diff with a set tolerance |
| Dependents | Are values derived from the targets still correct? | Formula dependents; NOT\_VERIFIED when they recalculate only on opening |
| Policy | Were signatures, permissions and authority respected? | Re-run policy on the released edit set; signature status unchanged |
| Provenance | Is the copy linked to its source and approvals? | Manifest complete; provenance embedded; hashes match |

**Outcomes.** Integrity, intent, preservation, policy and provenance are critical for every adapter. Visual is critical for PDF forms and is critical for DOCX when the server selects `structural_and_visual`; it is explicitly not performed in the DOCX `structural` profile. Dependents is never critical. Browser requests cannot select or weaken the server profile.

| Outcome | When | Download |
| --- | --- | --- |
| VERIFIED | Every check PASS | Released as a verified copy |
| STRUCTURALLY_VERIFIED | Every mandatory DOCX structural/content check PASS under the `structural` profile; visual checks explicitly `NOT_PERFORMED` | Released as a structurally verified DOCX with an explicit visual-not-checked warning |
| INCOMPLETE | No FAIL; every critical check PASS; at least one non-critical check NOT\_VERIFIED | Only if tenant policy allows unverified derivatives; labelled verification incomplete, with the unverified items listed |
| BLOCKED | Any FAIL, or any critical check NOT\_VERIFIED | None; the report is shown |

**Data corrections** are stored with actor, reason and time, shown in the Summary and Edit tabs, and exportable as a JSON or CSV sidecar linked to the source version. They never create a new version of the file.

## Versioning and concurrency

Every copy names the exact version it came from, and a stale edit can never be applied silently.

- **Version tree.** The original upload is the root version. Each released edit set creates a derived version whose parent is its base. Versions are immutable.
- **Base version.** A draft is created against a base: by default the document's current version, which is the latest released copy, or the original if none exists. The user may choose the original or any earlier released version instead.
- **Optimistic concurrency.** Each document has a `current_version_id`. Submitting a draft whose base is not current returns `409 stale_version`.
- **Rebase never rewrites a precondition.** Rebasing moves a draft to the current version and checks each patch against it:
  - **Clean:** the target's value and fingerprint in the current version still equal the patch's `expected_old_value` and `expected_fingerprint`. The patch carries over unchanged.
  - **Conflict:** they differ, for example the draft expects 45,000 and another release changed it to 60,000. The conflict is shown with all three values (original expected, current, proposed). Only the user can resolve it, by re-confirming the patch against the new value or dropping it.
  - Any resolved conflict creates a new draft revision with a new hash, and any earlier approval is void.
- **Branches.** Instead of rebasing, the user may confirm a branch from the older base. The version tree shows it, and it never becomes current automatically.
- **Patch preconditions.** Even on a current base, preflight rejects a patch whose `expected_old_value` or `expected_fingerprint` no longer matches its target.
- **Legacy review records.** Historical reviews remain immutable and readable. They are not consulted to authorize new releases; changing a legacy pending edit requires a new draft and current-source preflight.
- **Targets are per version.** Edit targets are recomputed for each version and never reused across versions.
- **Reproducibility.** Every version records the adapter, template and provider versions that produced its targets, fields and output.

## Security baseline

All of this ships in the first milestone. Retrofitting tenancy or audit later means rewriting every query.

**Tenancy.**

- `tenant_id` is on every table.
- **Row-level security on every tenant table,** with both `USING` and `WITH CHECK` policies comparing `tenant_id` to `current_setting('app.tenant_id')`. Tables also use `FORCE ROW LEVEL SECURITY`.
- **Database roles.** Migrations run as an owner role. The API and workers connect as a separate application role that does not own the tables and has `NOBYPASSRLS`. Owners and superusers bypass RLS, so they are never used by application code.
- **Tenant context.** Every transaction begins with `SET LOCAL app.tenant_id = …`, which is transaction-scoped and so cannot leak across pooled connections. The API sets it from the authenticated user. Each job payload carries its `tenant_id`, and the worker sets it per transaction itself; it never relies on request state.
- **Repository layer** also filters by tenant, so RLS is a second line of defence, not the only one. RLS tests run under the application role, not a privileged user.
- Object keys are prefixed by tenant, and caches are keyed by tenant plus adapter, template and provider versions.
- No cross-tenant deduplication. Two tenants uploading identical bytes get separate records and separate stored copies.
- A request for another tenant's id returns 404, never 403, so ids cannot be probed.

**Roles.**

| Role | Can |
| --- | --- |
The active product accepts one authenticated OWNER per tenant. The local demonstration session issuer is available only when explicitly enabled in loopback development and emits a short-lived server-signed identity. Future OIDC identities must map to OWNER authorization server-side. The browser cannot choose a role or tenant. Tenant isolation, RLS, category policy and format-specific edit restrictions apply to every operation. There is no active reviewer or administrator identity in the product workflow.

**Hostile files.**

- Type is detected from content and checked against the adapter allowlist.
- Limits on size, pages, zip expansion ratio and nesting depth apply; working targets are 50 MB and 200 pages, to be confirmed by benchmark.
- Every upload is scanned with ClamAV and quarantined on a match.
- Workers run as non-root with per-job CPU, memory and time limits, and with no network access.
- Plugin workers that need a provider endpoint run in a separate profile with only that endpoint allowed.
- LibreOffice runs with macros disabled in a fresh profile per job.
- Email HTML renders in a sandboxed iframe with scripts and remote content blocked.

**Plugins and prompt injection.** Document text is untrusted input to any plugin. Plugins return proposals only. They have no access to adapters, policy, storage or export, and cannot create patches. Which providers may receive document text is set per tenant.

**Audit.**

- `audit_events` is append-only: the application role has no UPDATE or DELETE grant.
- Each row stores the hash of the previous row.
- A daily digest of the chain head is signed and written to write-once storage, such as S3 Object Lock, so a database administrator cannot rewrite history unnoticed. The hook ships in v1; enabling it is required before production use.

**Data protection.** TLS everywhere, encryption at rest for database and objects, short-lived signed download links, and a retention and deletion policy per tenant. Provenance in copies is a convenience, since metadata can be stripped. The change manifest in the database is the authority.

## Data model

One Postgres schema; every table carries `tenant_id`, and nothing reviewed is ever overwritten in place.

| Table | Purpose | Key columns |
| --- | --- | --- |
| tenants | Organisations | id, name, policy settings |
| users | People and roles | id, tenant\_id, email, role |
| files | Stored bytes | id, tenant\_id, sha256, size, mime, storage\_key, role (original, preview, derived, sidecar) |
| documents | One per upload | id, tenant\_id, filename, suggested\_category, authorized\_category, authorized\_by, capability\_level, capability\_reasons, current\_version\_id, created\_by |
| document\_versions | Immutable version tree | id, document\_id, parent\_version\_id, kind, file\_id, sha256, edit\_set\_id, adapter\_version |
| blocks | Parsed structure and evidence | id, version\_id, kind, page, ord, text, bbox, locator |
| edit\_targets | Per-version targets | id, version\_id, kind, locator, current\_value, value\_type, editable, reason, dependents |
| templates | Versioned template definitions | id, tenant\_id, key, version, yaml, active |
| fields | Field meanings | id, document\_id, version\_id, key, label, source, provider\_version, target\_id, observed\_value, evidence, signals, review\_status |
| findings | Rule results | id, field\_id, rule, kind (value wrong, inconsistent, located wrongly), detail |
| data\_corrections | Value-only corrections | id, field\_id, corrected\_value, reason, actor\_id, created\_at |
| edit\_sets | Document corrections | id, document\_id, base\_version\_id, status (draft, ready_for_release, released, failed; legacy states retained), frozen hash, created\_by |
| patches | Exact changes | id, edit\_set\_id, target\_id, expected\_old\_value, new\_value |
| reviews | Historical approvals, retained for compatibility | Historical rows are preserved; the active owner workflow neither writes nor reads them as release authorization |
| verification\_results | Check outcomes | id, edit\_set\_id, check, status, detail |
| export\_manifests | Release record | edit\_set\_id, output\_version\_id, manifest |
| jobs | Pipeline work | id, document\_id, stage, status, attempts, error, started\_at, finished\_at |
| audit\_events | Append-only trail | id, tenant\_id, ts, actor\_id, action, subject, payload, prev\_hash, hash |

Object storage keys:

```
{tenant}/originals/{document_id}/{sha256}
{tenant}/derived/{document_id}/{version_id}/{filename}
{tenant}/previews/{version_id}/page-{n}.png
{tenant}/sidecars/{document_id}/{export_id}.json
```

## API

REST under `/api/v1`. Every route is authenticated and tenant-scoped as OWNER, and data corrections and document corrections have separate routes, so the two are never confused.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | /documents | Upload (streamed multipart); returns id and initial capability level |
| GET | /documents/{id} | Metadata, capability profile with reasons, category, current version |
| PATCH | /documents/{id} | Set suggested category; authenticated owner may set authorized category, with category restrictions still enforced |
| GET | /documents/{id}/events | Server-sent events for pipeline stages |
| GET | /documents/{id}/versions | Version tree |
| GET | /versions/{id}/preview | Viewer manifest for that version |
| GET | /versions/{id}/targets | Edit targets with editability and reasons |
| POST | /versions/{id}/targets/resolve | Selection in, edit target or reason out (manual mode) |
| GET | /documents/{id}/fields | Fields with evidence, signals, findings, review status |
| POST | /documents/{id}/fields | Create a manual field from a resolved selection |
| POST | /documents/{id}/templates/apply | Match and apply a template; returns proposed fields for confirmation |
| POST | /fields/{id}/data-corrections | Record a data correction |
| GET | /documents/{id}/data-corrections/export | Sidecar export, JSON or CSV |
| POST | /documents/{id}/edit-sets | Create a draft on a base version |
| PUT | /edit-sets/{id}/patches | Replace the draft's patches |
| POST | /edit-sets/{id}/submit | Freeze, hash, run policy; `409 stale_version` if the base is not current |
| POST | /edit-sets/{id}/rebase | Move the draft to the current version and re-run preflight |
| POST | /edit-sets/{id}/release | Owner-authorized independent verification, malware scan and release; no human approval |
| GET | /edit-sets/{id} | Status, preflight report, verification results, manifest |
| GET | /edit-sets/{id}/download | Short-lived signed link; refused unless released |
| GET | /documents/{id}/audit | Audit trail for the document |
| GET, PUT | /admin/policies, /admin/templates, /admin/users | Administration |

Errors share one shape: `{"code": "preflight_failed", "message": "...", "details": [{"target_id": "...", "reason": "expected_value_mismatch"}]}`. Active workflow codes include `stale_version`, `preflight_failed`, `not_editable`, `policy_denied`, `verification_failed` and `verification_incomplete`. Human approval routes and the `approval_required` outcome are retired.

## Frontend

The three tabs stay. What changes is that every screen states which version is shown, what the file allows, and which kind of correction an action makes.

**Persistent header.** Filename, version badge (Original, or Derived v3 from v2), capability badge (Editable, Read-only, Limited) with its reason on hover, and the document category.

**View tab.** A viewer per format family, each implementing `show(version)`, `highlight(evidence)` and `select() → Selection`:

| Format | Viewer |
| --- | --- |
| PDF, scans, images | pdf.js or image canvas, with box highlights |
| XLSX, CSV | Read-only grid (AG Grid Community), with cell selection |
| JSON, XML, TXT | Monaco in read-only mode, with range selection |
| DOCX, PPTX, legacy Office | pdf.js on the converted preview; selections map back through block text |
| Email | Sandboxed body plus attachment list |

**Summary tab.** In v1 this is the structural summary: document type or matched template, capability level, counts of pages, sheets or records, fields with their values, rule findings grouped by kind, data corrections, and version history. A plugin summary later appears as a clearly labelled extra section.

**Edit tab.** A split view: the field list on the left, the viewer on the right, with highlights synced both ways.

- **Add field:** select anything in the viewer, then name it. The field shows whether it has an editable target, and the reason if not.
- **Apply template:** shows proposed fields; the user confirms the match once.
- **Two actions on every field:**
  - *Correct data*: always available. Records the workbench value; the file is unchanged.
  - *Change in document*: enabled only when the target is editable, the file is Editable and policy allows. When disabled, it says why.
- **Draft panel:** lists document changes as old → new, the base version, and whether approval is needed.

**Save copy dialog.** Shows the patches, then the preflight results, then each verification check as PASS, FAIL or NOT\_VERIFIED, with a before-and-after preview. The download button reads Download verified copy, or Download copy, verification incomplete, which needs an extra confirmation. It is absent when the outcome is BLOCKED.

**State.** TanStack Query for server data. Drafts live on the server; the browser keeps only unsaved keystrokes in memory. One `EventSource` per open document.

## Stack and licensing

Everything in the core is under a permissive licence, or runs as a separate unmodified process. Two infrastructure pieces need a decision before commercial distribution.

| Layer | Choice | Licence and note |
| --- | --- | --- |
| Frontend | React, TypeScript, Vite, Tailwind CSS, shadcn/ui, TanStack Query | MIT |
| Viewers | pdf.js; AG Grid Community; Monaco | Apache-2.0; MIT; MIT |
| API | FastAPI, Pydantic, SQLAlchemy, Alembic | MIT |
| Jobs | RQ | BSD-2-Clause |
| Queue store | Valkey, Redis-compatible | BSD-3-Clause. Recent Redis releases are under source-available or AGPL terms; Valkey avoids the question |
| OOXML | lxml for writing; openpyxl for verification reads | BSD-3-Clause; MIT |
| PDF | pypdf for forms; pypdfium2 for text and rendering | BSD-3-Clause; Apache-2.0 or BSD-3-Clause, plus PDFium's own permissive licence |
| PDF syntax check | qpdf, as a separate process | Apache-2.0 |
| Previews and recalculation | LibreOffice headless, as a separate process | MPL-2.0 |
| Malware scan | ClamAV daemon, as a separate process | GPL-2.0; called over its socket, not linked |
| Database | PostgreSQL 16 | PostgreSQL licence |
| Object storage | Local filesystem in development; S3 or any S3-compatible service in deployment | MinIO is AGPL-3.0: fine for development, review before shipping it inside a product |
| Optional plugins | Docling; Tesseract or PaddleOCR; LLM providers through one client interface | MIT; Apache-2.0; provider terms |
| Not used | PyMuPDF | AGPL-3.0 or commercial; revisit only for digital-PDF redraw, under a commercial licence |

Run a full dependency licence scan (for example pip-licenses and license-checker in CI) before any commercial release.

## Acceptance tests

These tests are the specification. An implementation that passes by weakening a test, skipping a fixture or marking a check PASS without running it has not passed.

**Corpus.** At least 50 synthetic or properly licensed files. They come from several producers: Excel, LibreOffice and Google Sheets for XLSX; Word and LibreOffice for DOCX; Acrobat and LibreOffice for PDF forms. Each fixture has an expected-results JSON. The corpus lives in `tests/corpus/` and only grows.

**Two suites.**

- The **milestone suite** is every test for functionality built so far, plus all earlier gates. It must pass to close a milestone.
- The **full regression suite** is every applicable test for every implemented adapter. It runs before each release and when v1 scope is complete.
- A test for an adapter not built yet is never skipped. It asserts that the file gets an explicit Read-only or Limited level with reason `adapter_not_implemented`.

| ID | Area | Test | Pass condition |
| --- | --- | --- | --- |
| CORE-1 | Any file | Run the full pipeline on every fixture | Original SHA-256 unchanged; capability level and reason assigned; fixtures whose editable adapter is not built yet get Read-only or Limited with reason adapter\_not\_implemented |
| CORE-2 | Hostile files | Zip bomb, encrypted file, executable, EICAR test file, oversized file | Rejected or quarantined within limits; no worker crash |
| CORE-3 | Tenancy | Same file uploaded to two tenants | No shared records, versions or cache; cross-tenant ids return 404 |
| CORE-4 | Audit | Tamper with one audit row directly in the database | Chain verification reports the break |
| CSV-1 | CSV | No-op edit set | No derived version created; base version returned unchanged; attempt audited |
| CSV-2 | CSV | Edit one cell in UTF-8 BOM, Windows-1252 and CRLF files | Only that cell's bytes differ; encoding, delimiter, quoting and line endings kept |
| JSON-1 | JSON | Edit a nested number and a nested string | Types kept; key order kept; bytes outside value spans identical |
| XLSX-1 | XLSX | No-op edit set | No derived version created; base version returned unchanged; attempt audited |
| XLSX-2 | XLSX | Edit a string cell whose shared string is used by other cells | Only the target changes; other cells still show the old string |
| XLSX-3 | XLSX | Edit currency, date and boolean cells, in 1900 and 1904 date-system workbooks | Values and types read back; style attribute unchanged |
| XLSX-4 | XLSX | Edit a cell with formula dependents, direct and cross-sheet | Dependents listed; `fullCalcOnLoad` set; dependents check NOT\_VERIFIED; copy labelled incomplete |
| XLSX-5 | XLSX | Try to target a formula cell | Not editable, reason `formula_cell` |
| XLSX-6 | XLSX | Edit a workbook with a chart, image, comments, data validation, conditional formatting and defined names | All those parts unchanged; file opens in LibreOffice |
| XLSX-7 | XLSX | XLSM and encrypted XLSX | Read-only and Rejected respectively |
| PDF-1 | PDF form | Edit a text field | Value reads back; other fields unchanged; pixels outside the widget identical |
| PDF-2 | PDF form | Edit a checkbox, a radio group and a dropdown | Correct values and appearances |
| PDF-3 | PDF form | Signed PDF; certified PDF; XFA form | All Read-only |
| PDF-4 | PDF form | Value over `MaxLen` or wider than the widget | Preflight rejects with a reason |
| DOCX-1 | DOCX structural profile | Single-run edit in body, table cell, header and footer | Run properties unchanged; unlisted parts identical; exact targets and provenance pass; outcome `STRUCTURALLY_VERIFIED` |
| DOCX-2 | DOCX | Span crossing two runs; span in a hyperlink or field code | Not editable, with the right reason |
| DOCX-3 | DOCX | Tracked changes; protected document | Read-only |
| DOCX-4 | DOCX | First edit adds `docProps/custom.xml` | Only that part, `[Content_Types].xml` and `_rels/.rels` change |
| FLOW-1 | Concurrency | Submit a draft whose base is no longer current | `409 stale_version`; rebase re-runs preflight |
| FLOW-2 | Approval | Change an edit set after approval | Approval voided |
| FLOW-3 | Verification | Force a verification FAIL | No download link can be issued |
| FLOW-4 | Corrections | Make a data correction | No new version; file unchanged; correction in sidecar export |
| FLOW-5 | Plugins | Disable all plugins and run the milestone suite | All pass |
| TPL-1 | Templates | Apply the demo motor-claim template to its labelled fixtures | Every field located exactly; rule findings match expected |

**Added in v2.1.1.** Each joins the gate of the milestone that builds its area.

| ID | Area | Test | Pass condition |
| --- | --- | --- | --- |
| SEC-1 | Authority | Owner uploads a third-party document that resembles an insurer-authored form | Category remains an owner-audited classification; native corrections remain blocked unless category policy allows them, and file/target restrictions still apply |
| SEC-2 | Tenancy | A background job running as tenant A requests tenant B's rows and objects | No rows returned and object access refused, under the application role |
| SEC-3 | Tenancy | Application role tries to disable RLS or act as table owner | Refused |
| FLOW-6 | Rebase | Another release changes the same target before rebase | Conflict shown with expected, current and proposed values; precondition unchanged until the user resolves; approval void |
| FLOW-7 | Concurrency | Two edit sets from the same base verified at the same time | Exactly one becomes current; the other is recorded as a branch; no lost update |
| FLOW-8 | Crash | Kill the worker after staging, before commit | No version row and no download link; a retry creates exactly one version; the sweeper removes the orphan |
| FLOW-9 | Idempotency | Retry an export that already committed | The same version is returned; no duplicate |
| VERIFY-1 | Outcomes | A critical check cannot run, such as a PDF visual check or a DOCX strict-profile renderer | BLOCKED; no download |
| VERIFY-2 | Outcomes | XLSX edit with dependents, with the unverified-derivatives policy on and then off | On: INCOMPLETE, labelled, items listed. Off: no download |
| XLSX-8 | XLSX | Empty edit set on a workbook with no custom properties part | Base version returned; no derived file; no part added |
| XLSX-9 | XLSX | Edit a cell read by an `INDIRECT` formula | Dependents unknowable; outcome INCOMPLETE, never VERIFIED |
| XLSX-10 | XLSX | Dates, currency, percentages, a 16-digit number, leading spaces, a rich-text cell | Displays match the expected display; the 16-digit value is rejected; the rich-text cell is not editable |
| JSON-2 | JSON | Edit inside an object with duplicate keys | Not editable, reason `duplicate_keys` |
| JSON-3 | JSON | Edit strings next to non-ASCII and escaped characters | Byte spans correct; escaping style kept |
| CSV-3 | CSV | New values beginning with `=`, `+`, `@`, and a decimal -500 | The first three rejected with `unsafe_csv_value`; -500 accepted |
| DOCX-5 | DOCX strict visual profile | Single-run replacement long enough to wrap its paragraph | Visual check FAIL; outcome BLOCKED. Under structural-only mode, no layout-equivalence claim is made |
| PDF-5 | PDF form | Any field edit | Original bytes are an exact prefix of the output |

## Milestones

Each milestone closes when its gate tests pass, and every earlier gate must still pass. The hardest proof, a verified XLSX round trip, comes first as a standalone spike, before any infrastructure is built.

1. **M0 XLSX proof spike.** The core package only: no API, database or UI. A command-line tool runs `inspect`, `targets`, `apply` and `verify` on a fixture with shared strings, formulas, styles and an embedded image. It proves the hardest guarantee before any infrastructure exists, and its code becomes the M3 adapter. **Gate:** XLSX-1 to XLSX-4, XLSX-6, XLSX-8 and XLSX-9 on that fixture set. Then the copy is opened by hand in Excel, or LibreOffice if Excel is unavailable.
2. **M1 Foundation.** Upload, storage, versions, registry and `inspect`, viewers, tenancy with RLS, roles, sandboxed workers, ClamAV, audit chain, event stream. Benchmark the size and page limits on your machine. **Gate:** CORE-1 to CORE-4, SEC-2, SEC-3.
3. **M2 Native editing.** Manual field selection, data corrections and sidecar export, category authorisation, edit sets, policy, preflight, the staged release protocol, verifier, manifests, download, CSV and JSON adapters. **Gate:** CSV-1 to CSV-3, JSON-1 to JSON-3, FLOW-1 to FLOW-9, VERIFY-1, SEC-1.
4. **M3 XLSX.** The M0 adapter wired into the pipeline, plus the display checks and rich-text handling. **Gate:** XLSX-1 to XLSX-10, VERIFY-2.
5. **M4 Templates and rules.** Template format and matching, rules engine, findings, structural summary, one demo motor-claim worksheet template. **Gate:** TPL-1, and SEC-1 re-run through a template match.
6. **M5 Fillable PDF.** A fixture proof of pypdf's incremental mode and appearance regeneration first, then the full adapter: signature and certification detection, rendering diff. **Gate:** PDF-1 to PDF-5.
7. **M6 DOCX.** Single-run targets, exclusions, provenance and structural verification are the standard release path. Optional strict rendered-layout verification is server-selected and fail-closed. **Gate:** DOCX-1 to DOCX-4 cover structural behavior; DOCX-5 covers the strict visual profile.
8. **M7 Optional plugins.** OCR for scans, Docling parsing, LLM field discovery and summary, each behind its interface. **Gate:** FLOW-5 still passes with plugins disabled; plugin field proposals are measured against labelled fixtures for precision and recall per field.

When handing a milestone to Claude Code or Codex, give it this document, the milestone's scope, and its gate test IDs. State that the tests define done and that no test may be weakened or skipped to make it pass.

## Deferred decisions

None of these block M1 to M3.

- [ ] Line of business beyond the demo motor template
- [ ] Deployment model, and where plugins and their models run
- [ ] Default for visible edited stamps on copies
- [ ] Provider for the external audit anchor
- [ ] Retention periods per tenant
- [ ] Final file size and page limits, from the M1 benchmark
- [ ] Write-back for multi-run DOCX, PPTX and EML, each after its own test corpus
- [ ] Digital-PDF text redraw, which needs a commercial PyMuPDF licence or another engine, plus a written authenticity policy
- [ ] Scanned-document write-back; current position: not built, data corrections and sidecars only

## Changes in v2.1.1

This revision fixes the contradictions and gaps found in the v2.1 technical review; the architecture is unchanged.

| Area | Change |
| --- | --- |
| Authority | `suggested_category` and `authorized_category` are separate; templates, plugins and uploaders can only suggest (SEC-1) |
| No-op | An empty or no-change edit set returns the base version and creates no derived file, so no-op tests stay byte-identical (XLSX-8) |
| Rebase | Rebase never rewrites `expected_old_value`; conflicts are shown and resolved by the user, with a new hash and fresh approval (FLOW-6) |
| Release | Staged, content-addressed candidates; one locked transaction; conditional update of `current_version_id`; idempotency keys; orphan sweeper (FLOW-7 to FLOW-9) |
| Outcomes | VERIFIED, INCOMPLETE and BLOCKED; integrity, intent, preservation, policy and provenance are always critical (VERIFY-1, VERIFY-2) |
| Test suites | Milestone suite and full regression suite; unbuilt adapters are tested for explicit `adapter_not_implemented`, never skipped |
| Typed contracts | `TypedValue`, target fingerprints, typed `ChangePermission`, and the supporting types the adapters and export job exchange |
| CSV | Formula-injection policy (CSV-3) |
| JSON | UTF-8 only, duplicate keys not editable, byte-accurate spans (JSON-2, JSON-3) |
| XLSX | Defined formula subset, unknowable dependents, precision limit, rich text excluded, display checks, Excel qualification (XLSX-9, XLSX-10) |
| DOCX | Structural verification is mandatory by default; reflow detection is critical only under `structural_and_visual` (DOCX-5) |
| PDF | pypdf incremental mode adopted, proven on fixtures first (PDF-5) |
| Tenancy | RLS with `USING` and `WITH CHECK`, `FORCE ROW LEVEL SECURITY`, non-owner `NOBYPASSRLS` application role, transaction-local tenant context set by workers too (SEC-2, SEC-3) |
| Milestones | New M0 XLSX proof spike before any infrastructure; new tests added to gates |
