const API = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";
const SESSION_KEY = "channel360.demo.session";

export type FolderVersion = {
  version_id: string; number: number; kind: "original" | "derived"; name: string; created_at: string;
  size_bytes: number; parent_number: number | null; status: string; outcome: string | null;
};
export type Folder = {
  document_id: string; name: string; created_at: string; latest_version_id: string; demo: boolean; versions: FolderVersion[];
};
export type ChunkSource = { kind: string; label: string; pages?: number[]; sheet?: string; rows?: number[]; anchors?: { page?: number; y?: number; cell?: string; label: string }[] };
export type Chunk = { id: string; title: string; summary: string; words: number; points: string[]; method: string; source?: ChunkSource | null; facts?: { label: string; value: string; page?: number; cell?: string; blank?: boolean }[] };
export type Chunks = { sections: Chunk[]; word_count: number; text_available: boolean; excerpt?: string; excerpt_truncated?: boolean };

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}

let token = (() => { try { return sessionStorage.getItem(SESSION_KEY) ?? ""; } catch { return ""; } })();

async function login(): Promise<string> {
  const response = await fetch(`${API}/api/local-demo/session`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
  });
  if (!response.ok) throw new ApiError(response.status, "signin_unavailable", "The workspace is unavailable right now.");
  token = (await response.json()).access_token as string;
  try { sessionStorage.setItem(SESSION_KEY, token); } catch { /* storage may be blocked */ }
  return token;
}

export async function request(path: string, init: RequestInit = {}, retry = true): Promise<Response> {
  if (!token) await login();
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token}`);
  let response: Response;
  try { response = await fetch(`${API}${path}`, { ...init, headers }); }
  catch { throw new ApiError(0, "network", "The workspace could not be reached."); }
  if (response.status === 401 && retry) { token = ""; return request(path, init, false); }
  return response;
}

export async function json<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await request(path, init);
  if (!response.ok) {
    let body: { code?: string; message?: string } = {};
    try { body = await response.json(); } catch { /* generic */ }
    throw new ApiError(response.status, body.code ?? "error", body.message ?? "The request could not be completed.");
  }
  return response.json() as Promise<T>;
}

export const listFolders = (q = "") => json<{ items: Folder[] }>(`/api/folders${q ? `?q=${encodeURIComponent(q)}` : ""}`).then((r) => r.items);
export const getChunks = (documentId: string, versionId: string) => json<Chunks>(`/api/documents/${documentId}/versions/${versionId}/chunks`);
export const openSample = (key: string) => json<{ document_id: string; version_id: string }>(`/api/samples/${key}`, { method: "POST" });
export const openDemo = () => json<{ document_id: string; version_id: string }>(`/api/demo`, { method: "POST" });

export async function fetchVersionFile(documentId: string, versionId: string): Promise<Blob> {
  const response = await request(`/api/documents/${documentId}/versions/${versionId}/file`);
  if (!response.ok) {
    let body: { code?: string; message?: string } = {};
    try { body = await response.json(); } catch { /* generic */ }
    throw new ApiError(response.status, body.code ?? "file_unavailable", response.status === 409 && body.message ? body.message : "The file could not be opened.");
  }
  return response.blob();
}

export type ArchiveEntry = { path: string; name: string; folder: string; status: "imported" | "linked_duplicate" | "skipped" | "blocked" | "failed";
  reason: string | null; reason_text: string | null; document_id: string | null; version_id: string | null; sha256: string | null };
export type ArchiveReport = { archive: { name: string; sha256: string; size_bytes: number };
  summary: { entries: number; imported: number; linked_duplicate: number; skipped: number; blocked: number; failed: number; recognized: number };
  entries: ArchiveEntry[] };
export async function importArchive(file: File): Promise<ArchiveReport> {
  const response = await request(`/api/archives?filename=${encodeURIComponent(file.name)}`, { method: "PUT", body: file });
  if (!response.ok) {
    let body: { message?: string } = {};
    try { body = await response.json(); } catch { /* generic */ }
    throw new ApiError(response.status, "archive_failed", body.message ?? "This archive could not be imported.");
  }
  return response.json() as Promise<ArchiveReport>;
}

/** One plain-language sentence (plus the first few problems) for an archive import. */
export function archiveMessage(report: ArchiveReport): { text: string; problem: boolean } {
  const { summary: s, archive } = report;
  const parts = [`${archive.name}: ${s.recognized} document${s.recognized === 1 ? "" : "s"} found`];
  const done = [s.imported ? `${s.imported} added` : "", s.linked_duplicate ? `${s.linked_duplicate} already in your workspace` : ""].filter(Boolean);
  if (done.length) parts.push(done.join(", "));
  const trouble = report.entries.filter((e) => e.status === "blocked" || e.status === "failed" || e.status === "skipped");
  let text = parts.join(" — ") + ".";
  if (trouble.length) {
    const shown = trouble.slice(0, 3).map((e) => `${e.name} (${e.reason_text ?? "not imported"})`).join("; ");
    text += ` Not imported: ${shown}${trouble.length > 3 ? ` and ${trouble.length - 3} more` : ""}.`;
  }
  return { text, problem: trouble.some((e) => e.status !== "skipped") || s.recognized === 0 };
}

export async function uploadAndInspect(file: File, step: (n: number) => void): Promise<{ document_id: string; version_id: string; duplicate_of?: { document_id: string; name: string } | null }> {
  step(0);
  const upload = await request(`/api/documents?filename=${encodeURIComponent(file.name)}`, { method: "PUT", body: file });
  if (!upload.ok) {
    let body: { message?: string } = {};
    try { body = await upload.json(); } catch { /* generic */ }
    throw new ApiError(upload.status, "upload_failed", body.message ?? "This file could not be added.");
  }
  const registered = await upload.json() as { document_id: string; version_id: string; duplicate_of?: { document_id: string; name: string } | null };
  step(1);
  const inspected = await request(`/api/documents/${registered.document_id}/inspect`, { method: "POST" });
  if (!inspected.ok) throw new ApiError(inspected.status, "inspect_failed", "This file could not be read safely.");
  const report = await inspected.json() as { state?: string };
  if (report.state && report.state !== "ready") throw new ApiError(422, "not_ready", "This file could not be read safely.");
  return registered;
}

export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.style.display = "none";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

export function humanDate(date: string) {
  return new Date(date).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}
export function size(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}
export function ext(filename: string) { return filename.split(".").pop()!.toLowerCase(); }
const LABELS: Record<string, string> = { pdf: "PDF", docx: "DOCX", doc: "DOC", xlsx: "XLSX", xls: "XLS", pptx: "PPTX", md: "MD", markdown: "MD", txt: "TXT", text: "TXT", json: "JSON", csv: "CSV", html: "HTML", htm: "HTML", xml: "XML", rtf: "RTF" };
export function typeLabel(name: string) { return LABELS[ext(name)] ?? "FILE"; }
export function folderName(name: string) { return name.replace(/\.[^.]+$/, "").replace(/_/g, " "); }

export type Cell = { v: string; t: string; id: string; fp: string; e: boolean; r: string | null } | null;
export type CellStyle = { b?: 1; i?: 1; u?: 1; sz?: number; c?: string; bg?: string; ha?: string; va?: string; wrap?: 1; bl?: 1; br?: 1; bt?: 1; bb?: 1 };
export type SheetImage = { src: string; from: { col: number; row: number; dx: number; dy: number } | null; to?: { col: number; row: number; dx: number; dy: number } | null; ext?: { cx: number; cy: number } };
export type SheetLayout = { default_col?: number; default_row?: number; merges: [number, number, number, number][]; cols: Record<string, number>; rows: Record<string, number>; cells: Record<string, number>; images: SheetImage[] };
export type Sheet = { name: string; rows: Cell[][]; truncated: boolean; layout?: SheetLayout };
export type DocxBlock = { id: string; kind: string; text: string; heading: number | null; table: { index: number; row: number; col: number } | null; target_id: string | null; fp: string | null; e: boolean; r: string | null };
export type PdfField = { id: string; name: string; type: string; value: unknown; options: string[]; max_length: number | null; fp: string; e: boolean; r: string | null; widgets: { page: number; rect: number[]; state?: string | null }[] };
export type Preview = {
  name: string; is_latest: boolean; size_bytes: number; format: string;
  edit: { mode: "text" | "grid" | "docx" | "pdf" | "pdf_text" | "none"; reason: string | null };
  signature?: { status: string; validated: boolean }; styles?: CellStyle[];
  text?: string; html?: string | null; encoding?: string; kind?: string; sheets?: Sheet[]; blocks?: DocxBlock[]; slides?: string[][];
  pages?: { page: number; width: number; height: number; scanned: boolean }[]; fields?: PdfField[]; text_available?: boolean;
  render?: { available: boolean; engine: string; note: string; reason: string | null };
};
export const getPreview = (documentId: string, versionId: string) => json<Preview>(`/api/documents/${documentId}/versions/${versionId}/preview`);

export type PdfTextEdit = { id: string; old: string; new: string };
export type PdfRun = { id: string; page: number; text: string; bbox: [number, number, number, number]; font: string; size: number; e: boolean; r: string | null };
export type PdfPageText = { page: number; width: number; height: number; runs: PdfRun[] };
export const getPdfText = (documentId: string, versionId: string, page: number) =>
  json<PdfPageText>(`/api/documents/${documentId}/versions/${versionId}/pdf-text?page=${page}`);
export async function previewPdfEdits(documentId: string, versionId: string, edits: PdfTextEdit[]): Promise<{ bytes: ArrayBuffer; substituted: number }> {
  const response = await request(`/api/documents/${documentId}/versions/${versionId}/pdf-edit/preview`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pdf_edits: edits }) });
  if (!response.ok) {
    let body: { code?: string; message?: string } = {};
    try { body = await response.json(); } catch { /* generic */ }
    throw new ApiError(response.status, body.code ?? "error", body.message ?? "That change couldn't be applied safely.");
  }
  return { bytes: await response.arrayBuffer(), substituted: Number(response.headers.get("X-Preview-Substituted") ?? 0) };
}

export type SaveBody = { html?: string; changes?: { target_id: string; fingerprint: string; value: string | boolean }[]; text?: string; pdf_edits?: PdfTextEdit[] };
export type SaveResult = { version_id: string; outcome: string; note: string | null; version: FolderVersion | null };
export const saveVersion = (documentId: string, versionId: string, body: SaveBody) =>
  json<SaveResult>(`/api/documents/${documentId}/versions/${versionId}/save`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

/** Preview-only page rendering of an Office document (never the editing source, never a saved version). */
export async function fetchRenderBytes(documentId: string, versionId: string): Promise<ArrayBuffer> {
  const response = await request(`/api/documents/${documentId}/versions/${versionId}/render`);
  if (!response.ok) {
    let body: { code?: string; message?: string } = {};
    try { body = await response.json(); } catch { /* generic */ }
    throw new ApiError(response.status, body.code ?? "render_unavailable", body.message ?? "The page preview is unavailable.");
  }
  return response.arrayBuffer();
}
