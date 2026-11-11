const API = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";
const SESSION_KEY = "channel360.demo.session";

export type FolderVersion = {
  version_id: string; number: number; kind: "original" | "derived"; name: string; created_at: string;
  size_bytes: number; parent_number: number | null; status: string; outcome: string | null;
};
export type Folder = {
  document_id: string; name: string; created_at: string; latest_version_id: string; demo: boolean; versions: FolderVersion[];
};
export type Chunk = { id: string; title: string; summary: string; words: number; points: string[]; method: string };
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
  if (!response.ok) throw new ApiError(response.status, "file_unavailable", "The file could not be opened.");
  return response.blob();
}

export async function uploadAndInspect(file: File, step: (n: number) => void): Promise<{ document_id: string; version_id: string }> {
  step(0);
  const upload = await request(`/api/documents?filename=${encodeURIComponent(file.name)}`, { method: "PUT", body: file });
  if (!upload.ok) {
    let body: { message?: string } = {};
    try { body = await upload.json(); } catch { /* generic */ }
    throw new ApiError(upload.status, "upload_failed", body.message ?? "This file could not be added.");
  }
  const registered = await upload.json() as { document_id: string; version_id: string };
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
export type Sheet = { name: string; rows: Cell[][]; truncated: boolean };
export type DocxBlock = { id: string; kind: string; text: string; heading: number | null; table: { index: number; row: number; col: number } | null; target_id: string | null; fp: string | null; e: boolean; r: string | null };
export type PdfField = { id: string; name: string; type: string; value: unknown; options: string[]; max_length: number | null; fp: string; e: boolean; r: string | null; widgets: { page: number; rect: number[]; state?: string | null }[] };
export type Preview = {
  name: string; is_latest: boolean; size_bytes: number; format: string;
  edit: { mode: "text" | "grid" | "docx" | "pdf" | "none"; reason: string | null };
  text?: string; html?: string | null; encoding?: string; kind?: string; sheets?: Sheet[]; blocks?: DocxBlock[]; slides?: string[][];
  pages?: { page: number; width: number; height: number; scanned: boolean }[]; fields?: PdfField[]; text_available?: boolean;
};
export const getPreview = (documentId: string, versionId: string) => json<Preview>(`/api/documents/${documentId}/versions/${versionId}/preview`);

export type SaveBody = { html?: string; changes?: { target_id: string; fingerprint: string; value: string | boolean }[]; text?: string };
export type SaveResult = { version_id: string; outcome: string; note: string | null; version: FolderVersion | null };
export const saveVersion = (documentId: string, versionId: string, body: SaveBody) =>
  json<SaveResult>(`/api/documents/${documentId}/versions/${versionId}/save`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
