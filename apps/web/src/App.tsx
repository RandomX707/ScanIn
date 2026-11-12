import { StrictMode, useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import { ApiError, archiveMessage, importArchive, listFolders, openDemo, openSample, uploadAndInspect, type Folder } from "./lib/api";
import { Topbar, type View } from "./pages/Shell";
import { Home } from "./pages/Home";
import { FolderPage, History } from "./pages/Library";
import { Workspace } from "./pages/Workspace";

const MAX_BYTES = 30 * 1024 * 1024;
const STEPS = ["Read original file", "Extract readable content", "Build document map", "Prepare three views"];

type Progress = { name: string; step: number };
type Toast = { message: string; error: boolean } | null;

function App() {
  const [view, setView] = useState<View>("home");
  const [folders, setFolders] = useState<Folder[]>([]);
  const [folderId, setFolderId] = useState<string | null>(null);
  const [active, setActive] = useState<{ documentId: string; versionId: string; tab?: "original" | "chunks" | "edits" } | null>(null);
  const [search, setSearch] = useState("");
  const [progress, setProgress] = useState<Progress | null>(null);
  const [toast, setToast] = useState<Toast>(null);
  const busy = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const toastTimer = useRef<number | undefined>(undefined);

  const notify = useCallback((message: string, error = false) => {
    setToast({ message, error });
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 5000);
  }, []);

  const refresh = useCallback(async () => {
    try { setFolders(await listFolders()); } catch { /* the next action retries */ }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);

  function go(next: View) { setView(next); window.scrollTo(0, 0); if (next === "home" || next === "history") void refresh(); }
  function openWorkspace(documentId: string, versionId: string, tab?: "original" | "chunks" | "edits") {
    setActive({ documentId, versionId, tab }); setView("workspace"); window.scrollTo(0, 0);
  }
  function openFolder(documentId: string) { setFolderId(documentId); setView("folder"); window.scrollTo(0, 0); void refresh(); }
  const browse = () => { if (!busy.current) fileInput.current?.click(); };

  async function ingest(files: File[]) {
    if (busy.current || !files.length) return;
    busy.current = true;
    let last: { document_id: string; version_id: string } | null = null;
    let successes = 0;
    let archiveDocs = 0;
    let duplicateNote = "";
    let archiveNote: { text: string; problem: boolean } | null = null;
    for (const file of files.slice(0, 10)) {
      if (file.size > MAX_BYTES && !/\.zip$/i.test(file.name)) { notify(`${file.name}: 30 MB limit exceeded.`, true); continue; }
      setProgress({ name: file.name, step: 0 });
      if (/\.zip$/i.test(file.name)) {
        try {
          const report = await importArchive(file);
          const { text, problem } = archiveMessage(report);
          archiveNote = { text, problem };
          const docs = report.entries.filter((e) => e.document_id && e.version_id);
          if (docs.length === 1) last = { document_id: docs[0].document_id!, version_id: docs[0].version_id! };
          archiveDocs += docs.length;
        } catch (error) {
          notify(`${file.name}: ${error instanceof ApiError ? error.message : "Cannot import archive"}`, true);
        } finally { setProgress(null); }
        continue;
      }
      try {
        const registered = await uploadAndInspect(file, (step) => setProgress({ name: file.name, step }));
        setProgress({ name: file.name, step: 2 });
        await new Promise((resolve) => setTimeout(resolve, 160));
        setProgress({ name: file.name, step: 3 });
        await new Promise((resolve) => setTimeout(resolve, 220));
        last = registered; successes++;
        if (registered.duplicate_of) duplicateNote = `${file.name} is identical to “${registered.duplicate_of.name}”, which is already in your workspace.`;
      } catch (error) {
        notify(`${file.name}: ${error instanceof ApiError ? error.message : "Cannot read file"}`, true);
      } finally { setProgress(null); }
    }
    busy.current = false;
    await refresh();
    if (archiveNote) {
      // An archive adds several documents: show the whole set in History (a single one opens directly).
      if (last && !successes && archiveDocs === 1) openWorkspace(last.document_id, last.version_id); else if (archiveDocs) go("history");
      notify(archiveNote.text, archiveNote.problem);
      if (!successes) return;
    }
    if (successes && last) {
      openWorkspace(last.document_id, last.version_id);
      notify(`${successes} document${successes === 1 ? "" : "s"} added. Original preserved.${duplicateNote ? ` ${duplicateNote}` : ""}`);
    }
  }

  async function demo() {
    try { const d = await openDemo(); await refresh(); openWorkspace(d.document_id, d.version_id); }
    catch (error) { notify(error instanceof ApiError ? error.message : "The demo could not be opened.", true); }
  }
  async function format(key: string) {
    try {
      const d = await openSample(key); await refresh(); openWorkspace(d.document_id, d.version_id);
      notify("Sample opened as a document folder.");
    } catch (error) { notify(error instanceof ApiError ? error.message : "The sample could not be opened.", true); }
  }

  const filtered = search.trim()
    ? folders.filter((f) => [f.name, ...f.versions.map((v) => v.name)].join(" ").toLowerCase().includes(search.trim().toLowerCase()))
    : folders;
  const folder = folders.find((f) => f.document_id === folderId);

  useEffect(() => {
    const over = (e: DragEvent) => { if (e.dataTransfer?.types.includes("Files")) e.preventDefault(); };
    const drop = (e: DragEvent) => {
      const target = e.target as HTMLElement | null;
      if (!target?.closest("#dropzone") && e.dataTransfer?.files?.length) { e.preventDefault(); void ingest(Array.from(e.dataTransfer.files)); }
    };
    document.addEventListener("dragover", over); document.addEventListener("drop", drop);
    return () => { document.removeEventListener("dragover", over); document.removeEventListener("drop", drop); };
  });

  return (
    <div className="app">
      <input ref={fileInput} type="file" id="file-input" hidden multiple data-testid="file-input"
        accept=".zip,.pdf,.docx,.xlsx,.xls,.pptx,.odt,.png,.jpg,.jpeg,.gif,.webp,.bmp,.txt,.text,.md,.markdown,.csv,.json,.html,.htm,.xml,.rtf"
        onChange={(e) => { const picked = Array.from(e.target.files ?? []); e.target.value = ""; void ingest(picked); }} />
      <Topbar view={view} onHome={() => go("home")} onHistory={() => go("history")} onDemo={demo} onBrowse={browse} />
      {view === "home" && <Home folders={folders} onBrowse={browse} onFiles={(f) => void ingest(f)} onFormat={format} onFolder={openFolder} onHistory={() => go("history")} />}
      {view === "history" && <History folders={filtered} search={search} onSearch={setSearch} onBrowse={browse} onOpenFolder={openFolder} />}
      {view === "folder" && <FolderPage folder={folder} onHistory={() => go("history")} onOpen={(d, v) => openWorkspace(d, v)} />}
      {view === "workspace" && active && (
        <Workspace key={`${active.documentId}:${active.versionId}`} documentId={active.documentId} versionId={active.versionId} initialTab={active.tab}
          folders={folders} onHome={() => go("home")} onHistory={() => go("history")} onFolder={openFolder}
          onOpen={openWorkspace} onBrowse={browse} notify={notify} refresh={refresh} />
      )}
      {progress && (
        <div className="overlay" id="progress-overlay">
          <div className="process-modal">
            <div className="kicker">Scanning file</div>
            <h2 id="progress-title">{progress.name}</h2>
            <p>A local preview of how Channel360 turns an upload into a navigable document experience.</p>
            <div className="process-list">
              {STEPS.map((label, i) => (
                <div key={label} className={`process-step ${i < progress.step ? "done" : i === progress.step ? "current" : ""}`}>
                  <i>{i < progress.step ? "✓" : i === progress.step ? "●" : String(i + 1)}</i>{label}
                </div>
              ))}
            </div>
            <div className="progress-track"><div className="progress-bar" style={{ width: `${Math.max(3, Math.min(100, ((progress.step + 1) / 4) * 100))}%` }} /></div>
          </div>
        </div>
      )}
      {toast && <div className={`toast${toast.error ? " error" : ""}`} role="status">{toast.message}</div>}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
