import { Fragment, StrictMode, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import type { PDFDocumentProxy } from "pdfjs-dist";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import "./style.css";

type Role = "processor" | "reviewer" | "administrator";
type Page = "overview" | "documents" | "approvals" | "settings" | "workspace";
type Confirmation = { kind: "release" } | { kind: "review"; decision: "approved" | "rejected"; documentId?: string; edit?: EditSet };
type DocumentItem = { id: string; filename: string; ingestion_state: string; document_category?: string; current_version_id: string | null; created_at: string; current_version_kind?: string; version_count?: number };
type Target = { id: string; label: string | null; sheet: string; cell: string; fingerprint: string; value: { type: string; value: unknown }; editable: boolean; reason: string | null; field_type?: string; options?: string[]; max_length?: number | null; native_locator?: Record<string, unknown> };
type DocDetails = { page_count?: number; title?: string; author?: string; pages?: { page: number; width: number; height: number; text: string; scanned_or_image_only: boolean }[]; fields?: { id: string; name: string; type: string; value: { type: string; value: unknown }; editable: boolean; reason: string | null; options: string[]; max_length: number | null }[]; blocks?: { id: string; part: string; paragraph_index: number; text: string; kind: string; target_ids: string[] }[]; paragraph_count?: number; preview_note?: string };
type StructuralSummary = { source_document_id: string; source_version_id: string; source_sha256: string; format: string; apparent_document_type: string | null; description: string; statistics: Record<string, unknown>; observed_fields: { name: string; kind: string }[]; limitations: string[]; generation_method: "structural"; evidence_references: { reference: string; label: string }[] };
type Inspection = { media_type: string; capability: { level: string; reasons: string[] }; adapter: string | null; features: string[]; findings: string[]; source_sha256: string; viewing_ready: boolean; targets: Target[]; targets_truncated: boolean; state: string; details?: DocDetails };
type Field = { field_id: string; target_id: string; label: string; observed_value: { type: string; value: unknown } };
type Correction = { id: string; field_id: string; label: string; corrected_value: { type: string; value: unknown }; reason: string; actor_id: string; created_at: string };
type Viewed = { document_id: string; version_id: string; version_kind: string; filename: string; source_sha256: string; adapter: string; capability: { level: string; reasons: string[] }; targets: Target[]; details?: DocDetails; structural_summary: StructuralSummary | null };
type Version = { version_id: string; parent_version_id: string | null; kind: string; sha256: string; is_current: boolean; verification_outcome: string | null; edit_set_id: string | null; created_at?: string };
type EditSet = { edit_set_id: string; document_id?: string; filename?: string; created_by?: string; status: string; base_version_id?: string; frozen_hash?: string; outcome?: string; version_id?: string | null; download_available?: boolean; reason?: string; patches?: { target_id: string; expected_old_value: { type: string; value: unknown }; new_value: { type: string; value: unknown } }[]; changes?: { target_id: string; old: { type: string; value: unknown }; proposed: { type: string; value: unknown } }[]; verification?: { outcome: string; checks: { check: string; status: string; detail: Record<string, unknown> }[] } };
type Audit = { sequence: number; ts: string; actor_id: string; action: string; subject: string; payload: Record<string, unknown>; event_hash: string };
type Dashboard = { metrics: { total_documents: number; awaiting_inspection: number; documents_with_corrections: number; pending_approvals: number; verified_versions: number }; recent_documents: DocumentItem[]; recent_activity: { sequence: number; ts: string; actor_id: string; action: string; subject: string }[] };

const API = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";
const roleDetails: Record<Role, { label: string; description: string; initial: string }> = {
  processor: { label: "Document processor", description: "Inspect records, record corrections and prepare document changes.", initial: "P" },
  reviewer: { label: "Independent reviewer", description: "Review submitted changes and record an independent decision.", initial: "R" },
  administrator: { label: "Workspace administrator", description: "Manage document classification and perform processor actions.", initial: "A" },
};

function pretty(value: string) { return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase()); }
function summaryStatValue(key: string, value: unknown): string {
  if (Array.isArray(value)) return value.map((item) => {
    if (item && typeof item === "object") {
      const sheet = item as { name?: string; rows?: number; columns?: number; headers?: string[] };
      return `${sheet.name ?? "Worksheet"}: ${sheet.rows ?? 0} rows × ${sheet.columns ?? 0} columns${sheet.headers?.length ? ` · headings ${sheet.headers.join(", ")}` : ""}`;
    }
    return String(item);
  }).join("; ");
  if (value && typeof value === "object") return Object.entries(value as Record<string, unknown>).map(([name, count]) => `${pretty(name)}: ${count}`).join(" · ");
  if (key.endsWith("_bytes")) return `${Number(value).toLocaleString()} bytes`;
  return String(value);
}
function summaryStatName(key: string): string {
  return ({ record_count: "Data records", column_count: "Columns", top_level_structure: "Top-level shape",
    top_level_key_count: "Named fields", top_level_item_count: "Top-level items", worksheet_count: "Worksheets",
    page_count: "Pages", readable_text_pages: "Pages with readable text", scanned_or_image_only_pages: "Scanned or image-only pages",
    acroform_field_count: "Fillable form fields", paragraph_and_content_block_count: "Text blocks", sections_by_kind: "Content areas",
    worksheets: "Sheet details" } as Record<string, string>)[key] ?? pretty(key);
}
function editPriority(status: string) { return ({ submitted: 0, approved: 1, draft: 2, released: 3, rejected: 4, failed: 5 } as Record<string, number>)[status] ?? 6; }
function date(value?: string) { return value ? new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" }).format(new Date(value)) : "—"; }
function auditLabel(action: string) {
  const labels: Record<string, string> = {
    "document.original_registered": "Original document registered",
    "document.category_classified": "Document category classified",
    "document.derived_release_blocked": "Document release blocked",
    "document.derived_version_released": "Verified derivative released",
    "document.state_changed": "Document processing state changed",
    "edit_set.draft_created": "Native correction draft created",
    "edit_set.targets_added": "Correction values added",
    "edit_set.frozen_and_submitted": "Correction submitted for review",
    "edit_set.approved": "Correction approved by reviewer",
    "edit_set.rejected": "Correction rejected by reviewer",
    "field.data_corrected": "Data correction recorded",
    "field.manual_created": "Correction field added",
  };
  return labels[action] ?? "Workspace event recorded";
}
function columnNumber(reference: string): number | null {
  const letters = reference.match(/^[A-Z]+/i)?.[0];
  if (!letters) return null;
  return [...letters.toUpperCase()].reduce((total, letter) => total * 26 + letter.charCodeAt(0) - 64, 0);
}
function cellPosition(target: Target, adapter: string) {
  if (adapter === "csv") { const m = target.cell.match(/^(\d+),(\d+)$/); return m ? { row: Number(m[1]), column: Number(m[2]) } : null; }
  const row = target.cell.match(/[0-9]+$/)?.[0]; const column = columnNumber(target.cell);
  return row && column ? { row: Number(row), column } : null;
}
function Icon({ name }: { name: string }) {
  const paths: Record<string, string> = { overview: "M3 3h8v8H3zM13 3h8v5h-8zM13 10h8v11h-8zM3 13h8v8H3z", documents: "M6 2h8l5 5v15H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zm8 0v6h5M8 13h8M8 17h8", approvals: "M12 3l8 4v5c0 5-3.4 8-8 10-4.6-2-8-5-8-10V7l8-4zm-4 9 2.5 2.5L16 9", settings: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zm0-5v2m0 14v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M3 12h2m14 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4", search: "m20 20-4.5-4.5M18 10.5a7.5 7.5 0 1 1-15 0 7.5 7.5 0 0 1 15 0z", upload: "M12 16V4m0 0L7 9m5-5 5 5M4 16v4h16v-4", shield: "M12 3l8 4v5c0 5-3.4 8-8 10-4.6-2-8-5-8-10V7l8-4z", file: "M6 2h8l5 5v15H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zm8 0v6h5", clock: "M12 8v5l3 2m6-3a9 9 0 1 1-18 0 9 9 0 0 1 18 0z" };
  return <svg className="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] ?? paths.file} /></svg>;
}

function PdfDocumentViewer({ data, details, targets, selectedTarget, onSelect }: {
  data: Uint8Array | null; details?: DocDetails; targets: Target[]; selectedTarget: string | null; onSelect: (id: string) => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [documentProxy, setDocumentProxy] = useState<PDFDocumentProxy | null>(null);
  const [pdfjsModule, setPdfjsModule] = useState<typeof import("pdfjs-dist") | null>(null);
  const [page, setPage] = useState(1);
  const [zoom, setZoom] = useState(1);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    let loadingTask: import("pdfjs-dist").PDFDocumentLoadingTask | null = null;
    setDocumentProxy(null); setError(""); setPage(1);
    if (!data) return;
    void import("pdfjs-dist").then((module) => {
      if (!active) return;
      setPdfjsModule(module);
      module.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
      loadingTask = module.getDocument({ data: new Uint8Array(data), enableXfa: false, useWorkerFetch: false });
      return loadingTask.promise.then((loaded) => { if (active) setDocumentProxy(loaded); });
    }).catch(() => { if (active) setError("The browser PDF renderer could not safely open this file."); });
    return () => { active = false; void loadingTask?.destroy(); };
  }, [data]);
  useEffect(() => {
    let active = true;
    if (!pdfjsModule || !documentProxy || !canvas.current) return;
    void documentProxy.getPage(page).then(async (pdfPage) => {
      if (!active || !canvas.current) return;
      const box = canvas.current.parentElement?.getBoundingClientRect();
      const base = pdfPage.getViewport({ scale: 1 });
      const scale = Math.max(.45, Math.min(2.5, (box?.width ?? 680) / base.width * zoom));
      const viewport = pdfPage.getViewport({ scale });
      const context = canvas.current.getContext("2d");
      if (!context) return;
      canvas.current.width = Math.ceil(viewport.width); canvas.current.height = Math.ceil(viewport.height);
      await pdfPage.render({ canvasContext: context, viewport, annotationMode: pdfjsModule.AnnotationMode.ENABLE }).promise;
    }).catch(() => { if (active) setError("This page could not be rendered."); });
    return () => { active = false; };
  }, [pdfjsModule, documentProxy, page, zoom]);
  const formFields = details?.fields ?? [];
  return <div className="pdf-workspace">
    <div className="pdf-toolbar"><button className="secondary small-button" disabled={!documentProxy || page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>Previous</button><span>Page <b>{page}</b> of {details?.page_count ?? documentProxy?.numPages ?? "—"}</span><button className="secondary small-button" disabled={!documentProxy || page >= (documentProxy?.numPages ?? 1)} onClick={() => setPage((value) => Math.min(documentProxy?.numPages ?? 1, value + 1))}>Next</button><span className="toolbar-spacer" /><button className="secondary small-button" aria-label="Zoom out" onClick={() => setZoom((value) => Math.max(.65, value - .15))}>−</button><span>{Math.round(zoom * 100)}%</span><button className="secondary small-button" aria-label="Zoom in" onClick={() => setZoom((value) => Math.min(1.6, value + .15))}>+</button></div>
    {error ? <div className="preview-unavailable"><Icon name="file" /><p>{error}</p></div> : <div className="pdf-page-frame"><canvas ref={canvas} aria-label={`PDF page ${page}`} /></div>}
    <div className="pdf-field-list"><div className="section-heading"><div><h3>Detected form fields</h3><p>{formFields.length} AcroForm fields · scanned pages have no text edits</p></div></div>{formFields.length ? formFields.map((field) => {
      const target = targets.find((item) => item.id === field.id);
      return <button className={`pdf-field-row ${selectedTarget === field.id ? "selected" : ""}`} key={field.id} disabled={!target?.editable} onClick={() => target && onSelect(target.id)}><span><strong>{field.name}</strong><small>{field.type} · {String(field.value.value)}</small></span><span className={`field-capability ${field.editable ? "allowed" : "denied"}`}>{field.editable ? "Selectable" : pretty(field.reason ?? "read only")}</span></button>;
    }) : <div className="preview-note">No fillable AcroForm fields were found. The PDF remains read-only.</div>}</div>
  </div>;
}

function DocxDocumentViewer({ details, targets, selectedTarget, onSelect, nativeEditingAvailable }: {
  details?: DocDetails; targets: Target[]; selectedTarget: string | null; onSelect: (id: string) => void; nativeEditingAvailable: boolean;
}) {
  const blocks = details?.blocks ?? [];
  return <div className="docx-workspace"><div className="docx-render-note"><Icon name="shield" /><div><strong>Read-only extracted text</strong><p>Source formatting stays in the original DOCX. LibreOffice layout rendering is unavailable in this session, so document corrections cannot be released as verified.</p></div></div><div className="docx-block-list" aria-label="DOCX paragraphs and table text">{blocks.map((block) => <article className={`docx-block ${block.kind === "table_cell" ? "table-cell-block" : ""}`} key={block.id}><span className="eyebrow">{pretty(block.kind)} · {block.part.replace("word/", "")}</span><p>{block.text}</p>{block.target_ids.map((id) => { const target = targets.find((item) => item.id === id); if (!target) return null; return <button className={`text-target ${selectedTarget === id ? "selected" : ""}`} key={id} disabled={!target.editable} onClick={() => onSelect(id)}>{target.editable ? (nativeEditingAvailable ? "Select native target" : "Select for data correction") : `Read only · ${pretty(target.reason ?? "unsupported")}`}</button>; })}</article>)}</div>{!blocks.length && <div className="preview-note">No readable body, table, header or footer text was found.</div>}</div>;
}

function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem("channel360.demo.session") ?? "");
  const [role, setRole] = useState<Role | null>(() => sessionStorage.getItem("channel360.demo.role") as Role | null);
  const [tenantName, setTenantName] = useState("Local demo workspace");
  const [page, setPage] = useState<Page>("overview");
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [selected, setSelected] = useState<DocumentItem | null>(null);
  const [viewed, setViewed] = useState<Viewed | null>(null);
  const [pdfBytes, setPdfBytes] = useState<Uint8Array | null>(null);
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const [fields, setFields] = useState<Field[]>([]);
  const [corrections, setCorrections] = useState<Correction[]>([]);
  const [versions, setVersions] = useState<Version[]>([]);
  const [audit, setAudit] = useState<Audit[]>([]);
  const [editSet, setEditSet] = useState<EditSet | null>(null);
  const [selectedTarget, setSelectedTarget] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<"inspect" | "summary" | "data" | "native" | "history">("summary");
  const [activeSheet, setActiveSheet] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [roleChoice, setRoleChoice] = useState<Role>("processor");
  const [search, setSearch] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");
  const [stateFilter, setStateFilter] = useState("all");
  const [sort, setSort] = useState<"filename" | "created_at" | "ingestion_state">("created_at");
  const [dragging, setDragging] = useState(false);
  const [dataValues, setDataValues] = useState<Record<string, string>>({});
  const [dataReasons, setDataReasons] = useState<Record<string, string>>({});
  const [dataTypes, setDataTypes] = useState<Record<string, string>>({});
  const [nativeReason, setNativeReason] = useState("");
  const [patchValue, setPatchValue] = useState("");
  const [patchType, setPatchType] = useState("string");
  const [category, setCategory] = useState("unknown");
  const [approvalItems, setApprovalItems] = useState<{ document: DocumentItem; edit: EditSet }[]>([]);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const canProcess = role === "processor" || role === "administrator";
  const canReview = role === "reviewer" || role === "administrator";
  const nativeSupported = viewed?.capability.level === "editable" && viewed.adapter !== "docx";

  const request = useCallback(async (path: string, init: RequestInit = {}) => {
    const response = await fetch(`${API}${path}`, { ...init, headers: { Authorization: `Bearer ${token}`, ...init.headers } });
    if (!response.ok) { let error: { message?: string } = {}; try { error = await response.json(); } catch { /* generic API failure */ } if (response.status === 401) { sessionStorage.removeItem("channel360.demo.session"); sessionStorage.removeItem("channel360.demo.role"); setToken(""); setRole(null); setMessage(""); } throw new Error(error.message ?? `Request failed (${response.status})`); }
    return response;
  }, [token]);

  const loadDocuments = useCallback(async () => {
    if (!token) return;
    const response = await request("/api/documents?limit=100");
    const payload = await response.json() as { items: DocumentItem[] };
    setDocuments(payload.items);
  }, [request, token]);

  const loadDashboard = useCallback(async () => {
    if (!token) return;
    const response = await request("/api/dashboard/summary");
    setDashboard(await response.json() as Dashboard);
  }, [request, token]);

  const loadApprovals = useCallback(async (items = documents) => {
    if (!token || !canReview) { setApprovalItems([]); return; }
    const batches = await Promise.all(items.map(async (document) => {
      try {
        const response = await request(`/api/documents/${document.id}/edit-sets`);
        const result = await response.json() as { items: EditSet[] };
        return result.items.filter((edit) => edit.status === "submitted").map((edit) => ({ document, edit }));
      } catch { return []; }
    }));
    setApprovalItems(batches.flat());
  }, [canReview, documents, request, token]);

  useEffect(() => {
    if (!token) return;
    void Promise.all([loadDocuments(), loadDashboard()]).catch((error: Error) => setMessage(error.message));
  }, [token, loadDocuments, loadDashboard]);

  useEffect(() => { if (token && canReview) void loadApprovals(documents); }, [token, canReview, documents, loadApprovals]);
  useEffect(() => { const target = viewed?.targets.find((item) => item.id === selectedTarget); if (target) { setPatchType(target.value.type); setPatchValue(String(target.value.value ?? "")); } }, [selectedTarget, viewed]);

  async function signIn() {
    setBusy(true); setMessage("");
    try {
      const response = await fetch(`${API}/api/local-demo/session`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ role: roleChoice }) });
      if (!response.ok) throw new Error(response.status === 404 ? "Local demo sign-in is disabled on this server." : "The demo identity could not be issued.");
      const session = await response.json() as { access_token: string; role: Role; tenant_name: string };
      sessionStorage.setItem("channel360.demo.session", session.access_token);
      sessionStorage.setItem("channel360.demo.role", session.role);
      setToken(session.access_token); setRole(session.role); setTenantName(session.tenant_name); setPage("overview");
    } catch (error) { setMessage(error instanceof Error ? error.message : "Sign-in failed."); }
    finally { setBusy(false); }
  }

  function signOut() { sessionStorage.removeItem("channel360.demo.session"); sessionStorage.removeItem("channel360.demo.role"); setToken(""); setRole(null); setSelected(null); setViewed(null); setInspection(null); setFields([]); setCorrections([]); setVersions([]); setAudit([]); setEditSet(null); setApprovalItems([]); setConfirmation(null); setMessage(""); setPage("overview"); }

  async function selectDocument(item: DocumentItem) {
    setSelected(item); setPage("workspace"); setActiveTab("summary"); setViewed(null); setInspection(null); setSelectedTarget(null); setBusy(true); setMessage(""); setCategory(item.document_category ?? "unknown");
    try {
      let readyItem = item;
      if (item.ingestion_state !== "ready") {
        const inspectResponse = await request(`/api/documents/${item.id}/inspect?include_targets=true`, { method: "POST" });
        const checked = await inspectResponse.json() as Inspection; setInspection(checked);
        readyItem = { ...item, ingestion_state: checked.state }; setSelected(readyItem);
      }
      const versionId = readyItem.current_version_id;
      if (!versionId) throw new Error("A registered source version is not available yet.");
      const [viewR, fieldsR, correctionsR, versionsR, editsR, auditR] = await Promise.all([
        request(`/api/documents/${item.id}/versions/${versionId}/view`), request(`/api/documents/${item.id}/fields`), request(`/api/documents/${item.id}/corrections`), request(`/api/documents/${item.id}/versions`), request(`/api/documents/${item.id}/edit-sets`), request(`/api/documents/${item.id}/audit`),
      ]);
      const nextView = await viewR.json() as Viewed; setViewed(nextView); setPdfBytes(null);
      if (nextView.adapter === "pdf") {
        const content = await request(`/api/documents/${item.id}/versions/${versionId}/download`);
        setPdfBytes(new Uint8Array(await content.arrayBuffer()));
      }
      setFields((await fieldsR.json() as { items: Field[] }).items); setCorrections((await correctionsR.json() as { items: Correction[] }).items);
      setVersions((await versionsR.json() as { items: Version[] }).items); const editItems = (await editsR.json() as { items: EditSet[] }).items; setEditSet(editItems.sort((a, b) => editPriority(a.status) - editPriority(b.status))[0] ?? null); setAudit((await auditR.json() as { items: Audit[] }).items);
      setActiveSheet(nextView.targets[0]?.sheet || "CSV");
      setMessage("Source opened in read-only inspection. The original file is unchanged.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "The document could not be opened safely."); }
    finally { setBusy(false); }
  }

  async function upload() {
    if (!file || !canProcess) return;
    setBusy(true); setMessage("");
    try {
      const response = await request(`/api/documents?filename=${encodeURIComponent(file.name)}`, { method: "PUT", headers: { "Content-Type": file.type || "application/octet-stream" }, body: file });
      const created = await response.json() as { document_id: string; message: string };
      setMessage(`${file.name} uploaded and scanned. Inspection is required before viewing.`); setFile(null); await Promise.all([loadDocuments(), loadDashboard()]);
      const fresh = await request("/api/documents?limit=100"); const result = await fresh.json() as { items: DocumentItem[] };
      const newItem = result.items.find((item) => item.id === created.document_id); if (newItem) await selectDocument(newItem);
    } catch (error) { setMessage(error instanceof Error ? error.message : "Upload could not be completed."); }
    finally { setBusy(false); }
  }

  async function addField(target: Target) {
    if (!selected || !canProcess) return;
    try { const response = await request(`/api/documents/${selected.id}/fields`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ target_id: target.id, fingerprint: target.fingerprint }) }); const field = await response.json() as Field; setFields((old) => [...old, field]); setMessage("Field added to the correction workspace. Source bytes remain unchanged."); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Field could not be added."); }
  }

  async function saveDataCorrection(field: Field) {
    if (!selected || !canProcess) return;
    const reason = dataReasons[field.field_id] ?? ""; const raw = dataValues[field.field_id] ?? ""; const type = dataTypes[field.field_id] ?? field.observed_value.type;
    if (!reason.trim()) { setMessage("Add a reason before saving this correction."); return; }
    let value: unknown = raw;
    if (type === "integer" && !/^-?(0|[1-9]\d*)$/.test(raw)) { setMessage("Enter a whole number."); return; }
    if (type === "decimal" && !/^-?(0|[1-9]\d*)(\.\d+)?$/.test(raw)) { setMessage("Enter a valid decimal number."); return; }
    if (type === "boolean") { if (!["true", "false"].includes(raw.toLowerCase())) { setMessage("Enter true or false."); return; } value = raw.toLowerCase() === "true"; }
    if (type === "null") value = null;
    try { const response = await request(`/api/documents/${selected.id}/fields/${field.field_id}/corrections`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ corrected_value: { type, value }, reason }) }); const created = await response.json() as Correction; setCorrections((old) => [...old, { ...created, label: field.label }]); setDataValues((old) => ({ ...old, [field.field_id]: "" })); setMessage("Data correction saved separately. It does not alter the source document."); await Promise.all([loadDashboard(), loadDocuments()]); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Correction could not be saved."); }
  }

  async function classify() {
    if (!selected || role !== "administrator") return;
    try { await request(`/api/documents/${selected.id}/category`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ category }) }); setSelected({ ...selected, document_category: category }); setMessage("Document category recorded by the administrator. Classification alone does not grant edit permission."); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Category update was rejected."); }
  }

  async function createDraft() {
    if (!selected || !nativeReason.trim() || !canProcess) { setMessage("Enter a reason before creating the native correction draft."); return; }
    try { const response = await request(`/api/documents/${selected.id}/edit-sets`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason: nativeReason }) }); const created = await response.json() as EditSet; setEditSet({ ...created, patches: [] }); setMessage("Native correction draft created. The source remains immutable."); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Draft could not be created."); }
  }

  async function addNativePatch() {
    if (!selected || !editSet || !viewed || !selectedTarget || !canProcess) return;
    const target = viewed.targets.find((entry) => entry.id === selectedTarget); if (!target?.editable) { setMessage("Select an editable source value first."); return; }
    let value: unknown = patchValue;
    if (patchType === "integer") { if (!/^-?(0|[1-9]\d*)$/.test(patchValue)) { setMessage("Enter a whole number."); return; } value = patchValue; }
    if (patchType === "decimal") { if (!/^-?(0|[1-9]\d*)(\.\d+)?$/.test(patchValue)) { setMessage("Enter a valid decimal number."); return; } value = patchValue; }
    if (patchType === "boolean") { if (!["true", "false"].includes(patchValue.toLowerCase())) { setMessage("Enter true or false."); return; } value = patchValue.toLowerCase() === "true"; }
    if (patchType === "choice") value = patchValue;
    if (patchType === "null") value = null;
    try { await request(`/api/documents/${selected.id}/edit-sets/${editSet.edit_set_id}/patches`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ patches: [{ target_id: target.id, fingerprint: target.fingerprint, new_value: { type: patchType, value } }] }) }); setEditSet({ ...editSet, patches: [...(editSet.patches ?? []), { target_id: target.id, expected_old_value: target.value, new_value: { type: patchType, value } }] }); setMessage(`Native patch prepared for ${target.id}.`); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Patch could not be saved."); }
  }

  async function preflight() {
    if (!selected || !editSet || !canProcess) return;
    try { const response = await request(`/api/documents/${selected.id}/edit-sets/${editSet.edit_set_id}/preflight`, { method: "POST" }); const result = await response.json() as EditSet; setEditSet({ ...editSet, ...result }); setMessage(`Preflight complete: ${result.status}. Reviewer approval is required.`); await loadDashboard(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Preflight failed."); }
  }

  async function review(decision: "approved" | "rejected", documentId?: string, edit?: EditSet) {
    const target = edit ?? editSet; const docId = documentId ?? selected?.id;
    if (!target || !docId || !canReview) return;
    try { await request(`/api/documents/${docId}/edit-sets/${target.edit_set_id}/review`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision, note: `Reviewed in Channel360 as ${role}` }) }); setMessage(`Edit set ${decision}. The independent reviewer decision is recorded.`); if (selected?.id === docId) setEditSet({ ...target, status: decision }); await Promise.all([loadDocuments(), loadDashboard()]); const list = await request("/api/documents?limit=100"); await loadApprovals((await list.json() as { items: DocumentItem[] }).items); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Review decision was rejected."); }
  }

  async function release() {
    if (!selected || !editSet || !canProcess) return;
    try { const response = await request(`/api/documents/${selected.id}/edit-sets/${editSet.edit_set_id}/release`, { method: "POST" }); const result = await response.json() as EditSet; const status = result.outcome === "VERIFIED" ? "released" : result.outcome === "BLOCKED" ? "failed" : "incomplete"; setEditSet({ ...editSet, ...result, status }); setSelected({ ...selected, current_version_id: result.version_id ?? selected.current_version_id }); setMessage(result.outcome === "VERIFIED" ? "Independent verification passed. The verified derivative is ready." : `Verification ${result.outcome?.toLowerCase() ?? "unavailable"}. No verified download is available.`); const versionsResponse = await request(`/api/documents/${selected.id}/versions`); setVersions((await versionsResponse.json() as { items: Version[] }).items); await Promise.all([loadDocuments(), loadDashboard()]); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Release was blocked."); }
  }

  function confirmPendingAction() {
    const pending = confirmation;
    setConfirmation(null);
    if (pending?.kind === "release") void release();
    if (pending?.kind === "review") void review(pending.decision, pending.documentId, pending.edit);
  }

  async function downloadVersion(version: Version) {
    if (!selected) return;
    try { const response = await request(`/api/documents/${selected.id}/versions/${version.version_id}/download`); const blob = await response.blob(); const href = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = href; anchor.download = selected.filename; anchor.click(); URL.revokeObjectURL(href); setMessage("Authorized version download started."); }
    catch (error) { setMessage(error instanceof Error ? error.message : "This version is not available for download."); }
  }

  async function downloadOriginal() {
    if (!selected) return;
    try { const response = await request(`/api/documents/${selected.id}/original`); const blob = await response.blob(); const href = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = href; anchor.download = selected.filename; anchor.click(); URL.revokeObjectURL(href); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Original download is unavailable."); }
  }

  const filteredDocuments = useMemo(() => documents.filter((item) => item.filename.toLowerCase().includes(search.toLowerCase()) && (typeFilter === "all" || item.filename.toLowerCase().endsWith(`.${typeFilter}`)) && (stateFilter === "all" || item.ingestion_state === stateFilter)).sort((a, b) => sort === "filename" ? a.filename.localeCompare(b.filename) : sort === "ingestion_state" ? a.ingestion_state.localeCompare(b.ingestion_state) : new Date(b.created_at).getTime() - new Date(a.created_at).getTime()), [documents, search, stateFilter, sort, typeFilter]);
  const sheets = [...new Set(viewed?.targets.map((target) => target.sheet || "CSV") ?? [])];
  const gridRows = useMemo(() => {
    if (!viewed || viewed.adapter === "json") return [];
    const rows: Record<number, Target[]> = {};
    viewed.targets.filter((target) => (target.sheet || "CSV") === activeSheet).forEach((target) => { const pos = cellPosition(target, viewed.adapter); if (pos) (rows[pos.row] ??= []).push(target); });
    return Object.entries(rows).sort(([a], [b]) => Number(a) - Number(b)).map(([row, entries]) => ({ row, entries }));
  }, [activeSheet, viewed]);
  const messageText = message.toLowerCase();
  const messageTone = messageText.includes("incomplete") ? "warning-mark" : /could not|failed|blocked|rejected/.test(messageText) ? "error-mark" : "";
  const messageMark = messageTone ? "!" : "✓";

  if (!token || !role) return <main className="login-shell"><section className="login-brand"><div className="brand-mark large">C</div><span>CHANNEL360</span><h1>Document operations,<br />with a clear chain of custody.</h1><p>Review insurance records in a controlled workspace. Originals stay immutable while decisions and verified changes are recorded separately.</p><div className="login-security"><Icon name="shield" /> Local demonstration access · server-issued short-lived identity</div></section><section className="login-panel"><div className="login-top"><span>LOCAL WORKSPACE</span><span className="env-dot" /> Development environment</div><div className="login-form"><span className="eyebrow">Welcome to Channel360</span><h2>Choose your demo identity</h2><p>Each identity has a distinct role. Reviewer actions remain independent from the person who prepared a change.</p><div className="role-picker">{(Object.keys(roleDetails) as Role[]).map((option) => <button key={option} className={`role-option ${roleChoice === option ? "selected" : ""}`} onClick={() => setRoleChoice(option)} aria-pressed={roleChoice === option}><span className="role-avatar">{roleDetails[option].initial}</span><span><strong>{roleDetails[option].label}</strong><small>{roleDetails[option].description}</small></span><span className="radio-dot" /></button>)}</div><button className="primary wide" onClick={() => void signIn()} disabled={busy}>{busy ? <><span className="spinner" /> Issuing secure session…</> : "Enter workspace"}</button>{message && <p className="feedback error" role="status">{message}</p>}<p className="login-foot">Demo identities are issued by the local API and expire automatically. No signing key is present in the browser.</p></div><span className="login-version">Channel360 · Insurance document operations</span></section></main>;

  const nav = (id: Page, label: string, icon: string) => <button key={id} className={`nav-link ${page === id ? "active" : ""}`} onClick={() => { setPage(id); if (id !== "workspace") setSelected(null); }}><Icon name={icon} /><span>{label}</span>{id === "approvals" && approvalItems.length > 0 && <em>{approvalItems.length}</em>}</button>;
  const pageTitle = page === "overview" ? "Overview" : page === "documents" ? "Document library" : page === "approvals" ? "Review queue" : page === "settings" ? "Workspace settings" : "Document workspace";
  return <div className="app-shell">
    <aside className="sidebar"><div className="sidebar-brand"><div className="brand-mark">C</div><span><b>CHANNEL360</b><small>Document operations</small></span></div><div className="workspace-switch"><span className="tenant-avatar">{tenantName.slice(0, 1).toUpperCase()}</span><span><strong>{tenantName}</strong><small>Local workspace</small></span><span className="chevron">⌄</span></div><div className="nav-label">WORKSPACE</div><nav aria-label="Main navigation">{nav("overview", "Overview", "overview")}{nav("documents", "Documents", "documents")}{canReview && nav("approvals", "Review queue", "approvals")}</nav><div className="sidebar-bottom"><div className="secure-note"><Icon name="shield" /><span><strong>Protected workspace</strong><small>Tenant scope is verified by the API.</small></span></div><button className="nav-link" onClick={() => setPage("settings")}><Icon name="settings" /><span>Settings</span></button><div className="sidebar-user"><span className="user-avatar">{roleDetails[role].initial}</span><span><strong>{roleDetails[role].label}</strong><small>{role}</small></span><button className="icon-button" aria-label="Switch identity" title="Switch identity" onClick={signOut}>↗</button></div></div></aside>
    <main className="main-area"><header className="top-header"><div className="breadcrumb"><span>Workspace</span><b>/</b><strong>{pageTitle}</strong></div><div className="header-actions"><span className="live-indicator"><i /> Live workspace</span><button className="user-chip" onClick={signOut}><span className="user-avatar small">{roleDetails[role].initial}</span><span>{roleDetails[role].label}</span><span className="chevron">⌄</span></button></div></header>
      <div className="page-content">
        {page === "overview" && <><div className="page-heading"><div><span className="eyebrow">OPERATIONAL OVERVIEW</span><h1>Workspace overview</h1><p>Here is the current activity across {tenantName}.</p></div><button className="primary" onClick={() => setPage("documents")}><Icon name="upload" /> Add a document</button></div>
          <section className="metrics-grid" aria-label="Workspace document metrics"><Metric label="Total documents" value={dashboard?.metrics.total_documents} helper="Registered in this workspace" icon="documents" /><Metric label="Awaiting inspection" value={dashboard?.metrics.awaiting_inspection} helper="Private until inspection completes" icon="clock" tone="amber" /><Metric label="With data corrections" value={dashboard?.metrics.documents_with_corrections} helper="Sidecar corrections recorded" icon="file" /><Metric label="Pending approvals" value={dashboard?.metrics.pending_approvals} helper="Submitted for independent review" icon="approvals" tone="blue" /><Metric label="Verified versions" value={dashboard?.metrics.verified_versions} helper="Passed independent verification" icon="shield" tone="green" /></section>
          <div className="overview-columns"><section className="surface recent-card"><div className="section-heading"><div><h2>Recently added</h2><p>Original documents most recently registered.</p></div><button className="text-button" onClick={() => setPage("documents")}>View library <span>→</span></button></div><DocumentTable items={dashboard?.recent_documents ?? []} compact onSelect={selectDocument} /></section><section className="surface activity-card"><div className="section-heading"><div><h2>Recent activity</h2><p>Recorded audit events from this workspace.</p></div><Icon name="clock" /></div>{dashboard?.recent_activity.length ? <div className="activity-list">{dashboard.recent_activity.map((event) => <article className="activity-row" key={event.sequence}><span className="activity-dot" /><div><strong>{auditLabel(event.action)}</strong><small>{event.actor_id} · {date(event.ts)}</small><details><summary>Event evidence</summary><code>{event.action} · {event.subject} · sequence {event.sequence}</code></details></div></article>)}</div> : <Empty icon="clock" title="No activity yet" text="Workspace events will appear here as documents are processed." />}</section></div></>}
        {page === "documents" && <><div className="page-heading"><div><span className="eyebrow">DOCUMENT OPERATIONS</span><h1>Document library</h1><p>Find, inspect and manage source documents in your workspace.</p></div>{canProcess && <button className="primary" onClick={() => document.getElementById("upload-input")?.click()}><Icon name="upload" /> Upload document</button>}</div>
          {canProcess && <div className={`dropzone ${dragging ? "dragging" : ""}`} onDragOver={(event) => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); setFile(event.dataTransfer.files[0] ?? null); }}><input id="upload-input" className="visually-hidden" type="file" aria-label="Choose a document to upload" onChange={(event) => setFile(event.target.files?.[0] ?? null)} /><span className="drop-icon"><Icon name="upload" /></span><div><strong>{file ? file.name : "Drag a document here, or browse files"}</strong><small>{file ? `${(file.size / 1024).toFixed(0)} KB · scanned before registration` : "CSV, JSON, XLSX and other supported formats · malware scanned before inspection"}</small></div>{file ? <><button className="secondary" onClick={() => setFile(null)}>Remove</button><button className="primary" onClick={() => void upload()} disabled={busy}>{busy ? <><span className="spinner" /> Uploading…</> : "Upload securely"}</button></> : <button className="secondary" onClick={() => document.getElementById("upload-input")?.click()}>Browse</button>}</div>}
          <section className="surface library-surface"><div className="library-toolbar"><label className="search-box"><Icon name="search" /><input placeholder="Search by filename…" value={search} onChange={(event) => setSearch(event.target.value)} aria-label="Search documents" /></label><label className="filter-select"><span>Format</span><select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)}><option value="all">All formats</option><option value="csv">CSV</option><option value="json">JSON</option><option value="xlsx">XLSX</option><option value="pdf">PDF</option><option value="docx">DOCX</option></select></label><label className="filter-select"><span>Status</span><select value={stateFilter} onChange={(event) => setStateFilter(event.target.value)}><option value="all">All statuses</option>{[...new Set(documents.map((item) => item.ingestion_state))].map((value) => <option key={value}>{value}</option>)}</select></label><span className="result-count">{filteredDocuments.length} document{filteredDocuments.length === 1 ? "" : "s"}</span></div>{filteredDocuments.length ? <DocumentTable items={filteredDocuments} onSelect={selectDocument} sort={sort} onSort={setSort} /> : <Empty icon="documents" title={documents.length ? "No documents match these filters" : "Your library is ready"} text={documents.length ? "Try a different search term or filter." : "Upload a file to start a secure document workflow."} action={canProcess && !documents.length ? <button className="primary" onClick={() => document.getElementById("upload-input")?.click()}>Upload first document</button> : undefined} />}</section></>}
        {page === "approvals" && <><div className="page-heading"><div><span className="eyebrow">INDEPENDENT REVIEW</span><h1>Review queue</h1><p>Submitted native document changes awaiting an independent decision.</p></div><span className="count-pill">{approvalItems.length} awaiting review</span></div><section className="surface approval-list">{approvalItems.length ? approvalItems.map(({ document, edit }) => <article className="approval-card" key={edit.edit_set_id}><div className="approval-file-icon"><Icon name="file" /></div><div className="approval-details"><div className="approval-title"><h2>{document.filename}</h2><Status value={edit.status} /></div><p>{edit.reason || "Native document correction"}</p><div className="approval-meta"><span>Submitted by <b>{edit.created_by ?? "Document processor"}</b></span><span>Base version <code>{edit.base_version_id?.slice(0, 8) ?? "—"}</code></span><span>File state <b>{pretty(document.ingestion_state)}</b></span></div>{(edit.changes ?? edit.patches?.map((patch) => ({ target_id: patch.target_id, old: patch.expected_old_value, proposed: patch.new_value })))?.map((change) => <div className="diff-row" key={change.target_id}><code>{change.target_id}</code><span>{String(change.old.value)}</span><b>→</b><strong>{String(change.proposed.value)}</strong></div>)}<div className="approval-actions"><button className="secondary" onClick={() => void selectDocument(document)}>Inspect document</button><div><button className="secondary danger-text" onClick={() => setConfirmation({ kind: "review", decision: "rejected", documentId: document.id, edit })}>Reject</button><button className="primary" onClick={() => setConfirmation({ kind: "review", decision: "approved", documentId: document.id, edit })}>Approve change</button></div></div></div></article>) : <Empty icon="approvals" title="No changes awaiting review" text="Submitted edit plans appear here for an independent decision." />}</section></>}
        {page === "settings" && <><div className="page-heading"><div><span className="eyebrow">WORKSPACE</span><h1>Workspace settings</h1><p>Review the current local demonstration access and security boundaries.</p></div></div><section className="settings-grid"><article className="surface settings-card"><div className="settings-icon"><Icon name="shield" /></div><span className="eyebrow">ACCESS CONTEXT</span><h2>{tenantName}</h2><p>Identity, role and tenant scope are validated by the Channel360 API on every request.</p><dl><dt>Current role</dt><dd>{roleDetails[role].label}</dd><dt>Environment</dt><dd><span className="status-dot green" /> Local demo · loopback only</dd><dt>Session storage</dt><dd>Browser tab session · expires in 30 minutes</dd></dl><button className="secondary" onClick={signOut}>Switch demo identity</button></article><article className="surface settings-card"><div className="settings-icon pale-blue"><Icon name="documents" /></div><span className="eyebrow">DOCUMENT SAFEGUARDS</span><h2>Controls in effect</h2><ul className="control-list"><li><span className="status-dot green" /> Tenant-scoped database access</li><li><span className="status-dot green" /> Malware scan before document registration</li><li><span className="status-dot green" /> Immutable source versions</li><li><span className="status-dot green" /> Independent reviewer and verification checks</li></ul><p className="subtle-note">Administrative controls not supported by this local demo are intentionally not shown.</p></article></section></>}
        {page === "workspace" && selected && <><div className="workspace-heading"><button className="back-link" onClick={() => { setPage("documents"); setSelected(null); }}>← Document library</button><div className="workspace-title"><div className="file-badge">{selected.filename.split(".").pop()?.toUpperCase().slice(0, 4)}</div><div><span className="eyebrow">DOCUMENT WORKSPACE</span><h1>{selected.filename}</h1><div className="title-meta"><Status value={selected.ingestion_state} /><span>{pretty(selected.document_category ?? "unknown")} category</span><span>Added {date(selected.created_at)}</span></div></div><div className="workspace-head-actions"><button className="secondary" onClick={() => void downloadOriginal()}>Download original</button></div></div></div>
          <div className="workspace-layout"><section className="surface viewer-panel"><div className="viewer-toolbar"><div><h2>Source document</h2><p>{viewed ? `${viewed.adapter.toUpperCase()} · ${viewed.capability.level === "editable" ? "Native editing supported" : "Read only"}` : "Private source · inspection required"}</p></div><span className="immutable-badge"><Icon name="shield" /> Original protected</span></div>{busy && !viewed ? <div className="loading-state"><span className="spinner" /> Preparing safe preview…</div> : viewed ? <><div className="source-info"><span>Version <b>{viewed.version_id.slice(0, 8)}</b></span><code title={viewed.source_sha256}>SHA-256 {viewed.source_sha256.slice(0, 18)}…</code></div>{viewed.adapter === "pdf" ? <PdfDocumentViewer data={pdfBytes} details={viewed.details} targets={viewed.targets} selectedTarget={selectedTarget} onSelect={setSelectedTarget} /> : viewed.adapter === "docx" ? <DocxDocumentViewer details={viewed.details} targets={viewed.targets} selectedTarget={selectedTarget} onSelect={setSelectedTarget} nativeEditingAvailable={nativeSupported} /> : viewed.adapter === "json" ? <div className="json-tree" role="listbox" aria-label="JSON values">{viewed.targets.map((target) => <button key={target.id} role="option" aria-selected={selectedTarget === target.id} className={`json-node ${selectedTarget === target.id ? "selected" : ""}`} onClick={() => setSelectedTarget(target.id)}><span className="json-key">{target.cell || "/"}</span><span className="json-value-text">{String(target.value.value)}</span><small>{target.value.type}{!target.editable ? ` · ${target.reason ?? "read only"}` : ""}</small></button>)}</div> : <><div className="sheet-tabs" role="tablist" aria-label="Workbook sheets">{sheets.map((sheet) => <button role="tab" aria-selected={activeSheet === sheet} className={activeSheet === sheet ? "active" : ""} key={sheet} onClick={() => setActiveSheet(sheet)}>{sheet}</button>)}</div><div className="grid-container"><table className="source-grid"><thead><tr><th className="row-number">#</th>{[...new Set(gridRows.flatMap(({ entries }) => entries.map((entry) => cellPosition(entry, viewed.adapter)?.column).filter((column): column is number => column !== undefined)))].sort((a, b) => a - b).map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{gridRows.map(({ row, entries }) => <tr key={row}><th className="row-number">{row}</th>{[...new Set(entries.map((entry) => cellPosition(entry, viewed.adapter)?.column).filter((column): column is number => column !== undefined))].sort((a, b) => a - b).map((column) => { const target = entries.find((entry) => cellPosition(entry, viewed.adapter)?.column === column); return <td key={column}>{target && <button className={`grid-value ${selectedTarget === target.id ? "selected" : ""}`} title={`${target.id} · ${target.value.type}`} onClick={() => setSelectedTarget(target.id)}>{String(target.value.value)}</button>}</td>; })}</tr>)}</tbody></table></div></>}{selectedTarget && <div className="selected-source"><span>Selected value</span><strong>{selectedTarget}</strong>{canProcess && !fields.some((field) => field.target_id === selectedTarget) && <button className="text-button" onClick={() => { const target = viewed.targets.find((entry) => entry.id === selectedTarget); if (target) void addField(target); }}>Add to data corrections →</button>}</div>}</> : <div className="preview-unavailable"><Icon name="file" /><h3>Safe preview is unavailable</h3><p>This file remains stored privately. The current viewer supports structured CSV, JSON and XLSX files. No unsupported preview or editing controls are offered.</p>{selected.ingestion_state !== "ready" && canProcess && <button className="primary" onClick={() => void selectDocument(selected)}>Inspect safely</button>}</div>}</section>
            <aside className="surface inspector-panel"><div className="inspector-head"><div><span className="eyebrow">WORKING PANEL</span><h2>Document controls</h2></div><Status value={selected.ingestion_state} /></div><div className="inspector-tabs" role="tablist" aria-label="Document tools">{([["inspect", "Inspect"], ["summary", "Summary"], ["data", "Data corrections"], ["native", "Native changes"], ["history", "History"]] as const).map(([tab, title]) => <button role="tab" aria-selected={activeTab === tab} className={activeTab === tab ? "active" : ""} key={tab} onClick={() => setActiveTab(tab)}>{title}</button>)}</div>
              {activeTab === "inspect" && <div className="inspector-body"><h3>Inspection summary</h3>{inspection || viewed ? <><div className="summary-state"><span className="status-dot green" /><div><strong>Safe inspection complete</strong><small>Read-only inspection · Original preserved</small></div></div><dl className="summary-dl"><dt>Format</dt><dd>{inspection?.media_type ?? viewed?.adapter}</dd><dt>Capability</dt><dd>{inspection?.capability.level ?? viewed?.capability.level}</dd><dt>{viewed?.adapter === "docx" ? "Text targets" : "Editable values"}</dt><dd>{viewed?.adapter === "docx" ? viewed.targets.length : viewed?.targets.filter((item) => item.editable).length ?? inspection?.targets.filter((item) => item.editable).length ?? 0}</dd><dt>Source fingerprint</dt><dd className="hash-value">{viewed?.source_sha256 ?? inspection?.source_sha256}</dd></dl>{inspection?.findings?.length ? <div className="finding-box"><strong>Inspection notes</strong>{inspection.findings.map((finding) => <p key={finding}>{finding}</p>)}</div> : null}<button className="secondary full-width" onClick={() => void selectDocument(selected)}>Refresh inspection</button></> : <Empty icon="shield" title="Inspection not run" text="Inspect the document to safely extract its structure." action={canProcess ? <button className="primary" onClick={() => void selectDocument(selected)}>Inspect safely</button> : undefined} />}</div>}
              {activeTab === "summary" && <div className="inspector-body structural-summary-panel" data-testid="structural-summary">{viewed?.structural_summary ? <><div className="summary-state"><span className="status-dot green" /><div><strong>{viewed.structural_summary.apparent_document_type ?? "Document structure reviewed"}</strong><small>Deterministic summary · Read-only · Source version {viewed.version_id.slice(0, 8)}</small></div></div><p className="structural-description">{viewed.structural_summary.description}</p>{Object.keys(viewed.structural_summary.statistics).length > 0 && <><h3>Document structure</h3><dl className="summary-dl">{Object.entries(viewed.structural_summary.statistics).map(([key, value]) => <Fragment key={key}><dt>{summaryStatName(key)}</dt><dd>{summaryStatValue(key, value)}</dd></Fragment>)}</dl></>}{viewed.structural_summary.observed_fields.length > 0 && <><h3>Observed fields and sections</h3><ul className="summary-observed">{viewed.structural_summary.observed_fields.map((item, index) => <li key={`${item.kind}-${item.name}-${index}`}><span>{item.name}</span><small>{pretty(item.kind)}</small></li>)}</ul></>}{viewed.structural_summary.limitations.length > 0 && <div className="finding-box"><strong>Inspection limitations</strong>{viewed.structural_summary.limitations.map((item) => <p key={item}>{item}</p>)}</div>}<p className="summary-footnote">This structural description is tied to the immutable source hash. It does not classify the document for editing or authorize changes.</p></> : <Empty icon="file" title={busy ? "Preparing document summary" : "Summary unavailable"} text={busy ? "The restricted parser is examining the selected source version." : "No supported structural summary is available for this version."} />}</div>}
              {activeTab === "data" && <div className="inspector-body"><div className="tab-intro"><h3>Data corrections</h3><p>Record a corrected value and reason as an auditable sidecar. The original file never changes.</p></div>{fields.length ? fields.map((field) => <article className="correction-card" key={field.field_id}><div className="correction-title"><strong>{field.label || field.target_id}</strong><code>{field.target_id}</code></div><div className="value-compare"><span><small>Observed</small><b>{String(field.observed_value.value)}</b></span><span className="arrow">→</span><label><small>Corrected value</small><input aria-label="Corrected value" value={dataValues[field.field_id] ?? ""} onChange={(event) => setDataValues({ ...dataValues, [field.field_id]: event.target.value })} disabled={!canProcess} /></label></div><label className="form-label">Value type<select aria-label="Correction value type" value={dataTypes[field.field_id] ?? field.observed_value.type} onChange={(event) => setDataTypes({ ...dataTypes, [field.field_id]: event.target.value })} disabled={!canProcess}>{["string", "integer", "decimal", "boolean", "date", "datetime", "null"].map((item) => <option key={item} value={item}>{pretty(item)}</option>)}</select></label><label className="form-label">Reason<textarea aria-label="Correction reason" placeholder="Explain why this data needs correction" value={dataReasons[field.field_id] ?? ""} onChange={(event) => setDataReasons({ ...dataReasons, [field.field_id]: event.target.value })} disabled={!canProcess} /></label>{canProcess && <button className="primary full-width" onClick={() => void saveDataCorrection(field)}>Save data correction</button>}</article>) : <Empty icon="file" title="No correction fields" text="Select an editable value in the source, then add it here to record a data correction." />}{corrections.length > 0 && <><h3 className="subsection-title">Correction history</h3>{corrections.map((correction) => <div className="correction-history-row" key={correction.id}><strong>{correction.label}</strong><span>{String(correction.corrected_value.value)}</span><small>{correction.reason} · {correction.actor_id} · {date(correction.created_at)}</small></div>)}</>}</div>}
              {activeTab === "native" && <div className="inspector-body"><div className="tab-intro"><h3>Native document correction</h3><p>This creates a derived file. It requires an administrator-approved category, independent review, safe application and verification.</p></div>{viewed?.adapter === "docx" && <p className="notice" role="status">Native DOCX changes are disabled because isolated LibreOffice rendering is unavailable. DOCX cannot enter reviewer approval or produce a derivative until visual and layout verification is available. Use Data corrections for sidecar-only corrections.</p>}{role === "administrator" && <div className="classification-box"><label className="form-label">Document category<select value={category} onChange={(event) => setCategory(event.target.value)}><option value="unknown">Unknown</option><option value="insurer-authored">Insurer authored</option><option value="customer-form">Customer form</option><option value="third-party-issued">Third party issued</option><option value="signed-legal">Signed legal</option></select></label><button className="secondary" onClick={() => void classify()}>Set category (administrator only)</button></div>}{selected.document_category !== "insurer-authored" && <p className="notice">Native correction is blocked until an administrator classifies this as insurer-authored. Classification alone does not authorize a release.</p>}{editSet && <div className="workflow-status"><span className="eyebrow">CORRECTION PLAN</span><strong>{editSet.status === "incomplete" ? "Verification incomplete" : pretty(editSet.status)}</strong>{editSet.frozen_hash && <code>Plan {editSet.frozen_hash.slice(0, 16)}…</code>}{(editSet.changes ?? editSet.patches?.map((patch) => ({ target_id: patch.target_id, old: patch.expected_old_value, proposed: patch.new_value })))?.map((change) => <div className="diff-row" key={change.target_id}><code>{change.target_id}</code><span>{String(change.old.value)}</span><b>→</b><strong>{String(change.proposed.value)}</strong></div>)}{editSet.status === "draft" && canProcess && nativeSupported && <><label className="form-label">Reason for document change<textarea value={nativeReason} onChange={(event) => setNativeReason(event.target.value)} placeholder="Describe the source correction" /></label><button className="primary" onClick={() => void createDraft()}>Create correction draft</button>{editSet.patches && <><label className="form-label">Selected target value{viewed.targets.find((item) => item.id === selectedTarget)?.options?.length ? <select aria-label="Selected target value" value={patchValue} onChange={(event) => setPatchValue(event.target.value)}>{viewed.targets.find((item) => item.id === selectedTarget)?.options?.map((option) => <option key={option} value={option}>{option}</option>)}</select> : viewed.targets.find((item) => item.id === selectedTarget)?.value.type === "boolean" ? <select aria-label="Selected target value" value={patchValue} onChange={(event) => setPatchValue(event.target.value)}><option value="true">Checked</option><option value="false">Unchecked</option></select> : <input aria-label="Selected target value" value={patchValue} onChange={(event) => setPatchValue(event.target.value)} maxLength={viewed.targets.find((item) => item.id === selectedTarget)?.max_length ?? undefined} />}</label><label className="form-label">Value type<select aria-label="Native value type" value={patchType} onChange={(event) => setPatchType(event.target.value)}>{["string", "integer", "decimal", "boolean", "choice", "null"].map((item) => <option key={item} value={item}>{pretty(item)}</option>)}</select></label><button className="secondary" onClick={() => void addNativePatch()} disabled={!selectedTarget}>Add selected target patch</button><button className="primary" onClick={() => void preflight()} disabled={!editSet.patches.length}>Preflight and freeze</button></>}</>}{editSet.status === "submitted" && canReview && nativeSupported && <div className="action-pair"><button className="secondary danger-text" onClick={() => setConfirmation({ kind: "review", decision: "rejected" })}>Reject</button><button className="primary" onClick={() => setConfirmation({ kind: "review", decision: "approved" })}>Approve (reviewer)</button></div>}{editSet.status === "approved" && canProcess && nativeSupported && <button className="primary" onClick={() => setConfirmation({ kind: "release" })}>Apply, verify and release</button>}{editSet.verification && <div className={`verification-result ${editSet.verification.outcome === "VERIFIED" ? "success" : editSet.verification.outcome === "INCOMPLETE" ? "incomplete" : "blocked"}`}><strong>Independent verification: {editSet.verification.outcome}</strong><pre>{JSON.stringify(editSet.verification, null, 2)}</pre></div>}</div>}{!editSet && <div className="workflow-empty"><span className="workflow-step active">1</span><div><strong>Classify the source</strong><p>Administrator confirms the document is insurer-authored.</p></div><span className="workflow-step">2</span><div><strong>Prepare a frozen plan</strong><p>Processor selects an editable source value and preflights the proposed change.</p></div><span className="workflow-step">3</span><div><strong>Independent review</strong><p>A different reviewer approves or rejects the submitted plan.</p></div><span className="workflow-step">4</span><div><strong>Verify and release</strong><p>The system checks the derived file before allowing download.</p></div>{canProcess && nativeSupported && selected.document_category === "insurer-authored" && <><label className="form-label">Reason for document change<textarea value={nativeReason} onChange={(event) => setNativeReason(event.target.value)} placeholder="Describe the source correction" /></label><button className="primary" onClick={() => void createDraft()}>Create correction draft</button></>}</div>}</div>}
              {activeTab === "history" && <div className="inspector-body"><h3>Version history</h3><p className="tab-description">Original and derived files remain linked by immutable version records. Only independently verified derivatives can be downloaded.</p>{versions.map((version) => <article className="version-card" key={version.version_id}><div className={`version-icon ${version.kind === "derived" ? "blue" : ""}`}><Icon name="file" /></div><div className="version-data"><strong>{pretty(version.kind)} version {version.is_current && <span className="current-pill">Current</span>}</strong><small>{version.kind === "original" ? "Immutable source" : `Verification: ${version.verification_outcome ?? "pending"}`} · {version.version_id.slice(0, 12)}</small><code>{version.sha256}</code></div>{version.kind === "derived" && version.verification_outcome === "VERIFIED" ? <button className="secondary small-button" onClick={() => void downloadVersion(version)}>Download</button> : <span className="locked-label">{version.kind === "original" ? "Preserved" : version.verification_outcome === "INCOMPLETE" ? "Incomplete · locked" : version.verification_outcome === "BLOCKED" ? "Blocked · locked" : "Unavailable"}</span>}</article>)}<h3 className="subsection-title">Audit history</h3>{audit.length ? audit.map((event) => <article className="audit-card" key={event.sequence}><span className="audit-seq">{String(event.sequence).padStart(3, "0")}</span><div><strong>{auditLabel(event.action)}</strong><small>{event.actor_id} · {date(event.ts)}</small><details><summary>Technical evidence</summary><code>{event.action} · subject: {event.subject} · hash: {event.event_hash} · payload: {JSON.stringify(event.payload)}</code></details></div></article>) : <Empty icon="clock" title="No audit events" text="Recorded actions for this document will appear here." />}</div>}
            </aside></div></>}
      </div>{confirmation && <div className="modal-backdrop"><section className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-title"><span className="settings-icon pale-blue"><Icon name={confirmation.kind === "release" ? "shield" : "approvals"} /></span><h2 id="confirm-title">{confirmation.kind === "release" ? "Release verified document?" : `${confirmation.decision === "approved" ? "Approve" : "Reject"} this change?`}</h2><p>{confirmation.kind === "release" ? "Channel360 will apply the frozen plan, independently verify the derived file and make it available only if all checks pass." : "Your independent reviewer decision will be recorded in the document audit history."}</p><div><button className="secondary" onClick={() => setConfirmation(null)}>Cancel</button><button className="primary" onClick={confirmPendingAction}>{confirmation.kind === "release" ? "Confirm verified release" : `Confirm ${confirmation.decision === "approved" ? "approval" : "rejection"}`}</button></div></section></div>}{message && <div className="toast" role="status"><span className={`toast-mark ${messageTone}`}>{messageMark}</span><span>{message}</span><button aria-label="Dismiss notification" onClick={() => setMessage("")}>×</button></div>}<footer className="app-footer">Channel360 <span>·</span> Local demonstration workspace <span className="footer-lock"><Icon name="shield" /> Tenant-isolated</span></footer></main>
  </div>;
}

function Metric({ label, value, helper, icon, tone = "navy" }: { label: string; value?: number; helper: string; icon: string; tone?: string }) { return <article className="surface metric-card"><div className={`metric-icon ${tone}`}><Icon name={icon} /></div><span className="metric-label">{label}</span><strong className="metric-value">{value === undefined ? "—" : value.toLocaleString()}</strong><small>{helper}</small></article>; }
function Status({ value }: { value: string }) { const normalized = value.toLowerCase(); const tone = ["ready", "approved", "released", "verified", "scan_clean"].includes(normalized) ? "green" : ["failed", "rejected", "quarantined", "blocked"].includes(normalized) ? "red" : ["submitted", "scanning", "inspecting", "incomplete"].includes(normalized) ? "amber" : "gray"; const label = normalized === "ready" ? "Ready for inspection" : normalized === "released" ? "Verified derivative" : normalized === "incomplete" ? "Verification incomplete" : normalized === "submitted" ? "Awaiting review" : pretty(value); return <span className={`status-badge ${tone}`} aria-label={`Status: ${label}`}><i />{label}</span>; }
function Empty({ icon, title, text, action }: { icon: string; title: string; text: string; action?: ReactNode }) { return <div className="empty-state"><span className="empty-icon"><Icon name={icon} /></span><strong>{title}</strong><p>{text}</p>{action}</div>; }
function DocumentTable({ items, compact = false, onSelect, sort, onSort }: { items: DocumentItem[]; compact?: boolean; onSelect: (item: DocumentItem) => void; sort?: string; onSort?: (value: "filename" | "created_at" | "ingestion_state") => void }) { return <div className="table-wrap"><table className="document-table"><thead><tr><th><button className="sort-button" onClick={() => onSort?.("filename")}>Document {sort === "filename" ? "↕" : ""}</button></th><th>Category</th>{!compact && <th><button className="sort-button" onClick={() => onSort?.("created_at")}>Added {sort === "created_at" ? "↕" : ""}</button></th>}<th><button className="sort-button" onClick={() => onSort?.("ingestion_state")}>Status {sort === "ingestion_state" ? "↕" : ""}</button></th>{!compact && <th>Versions</th>}<th aria-label="Open" /></tr></thead><tbody>{items.map((item) => <tr key={item.id} onClick={() => onSelect(item)} tabIndex={0} onKeyDown={(event) => { if (event.key === "Enter") onSelect(item); }}><td><div className="table-file"><span className="file-type-icon">{item.filename.split(".").pop()?.toUpperCase().slice(0, 3)}</span><span><strong>{item.filename}</strong><small>{item.current_version_kind === "derived" ? "Latest version is derived" : "Original source preserved"}</small></span></div></td><td><span className="category-text">{pretty(item.document_category ?? "unknown")}</span></td>{!compact && <td>{date(item.created_at)}</td>}<td><Status value={item.ingestion_state} /></td>{!compact && <td><span className="version-count">{item.version_count ?? (item.current_version_id ? 1 : 0)} version{(item.version_count ?? 1) === 1 ? "" : "s"}</span></td>}<td><button className="row-open" aria-label={`Open ${item.filename}`} onClick={(event) => { event.stopPropagation(); onSelect(item); }}>→</button></td></tr>)}</tbody></table></div>; }

const root = document.getElementById("root");
if (!root) throw new Error("Missing root element");
createRoot(root).render(<StrictMode><App /></StrictMode>);
