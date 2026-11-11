import type { ReactNode } from "react";
import { Icon } from "../lib/icons";
import { folderName, typeLabel, humanDate, size as fmtSize, type Folder } from "../lib/api";

export type View = "home" | "history" | "folder" | "workspace";

export function Topbar({ view, onHome, onHistory, onDemo, onBrowse }: {
  view: View; onHome: () => void; onHistory: () => void; onDemo: () => void; onBrowse: () => void;
}) {
  return (
    <header className="topbar">
      <button className="brand" onClick={onHome} aria-label="Scan In home">
        <span className="brand-icon"><Icon name="layers" size={21} /></span>
        <span className="brand-name">Scan In<small>Channel360 / Document canvas</small></span>
      </button>
      <nav className="nav" aria-label="Primary navigation">
        <button className={view === "home" ? "active" : ""} onClick={onHome}>Scan In</button>
        <button className={view === "history" ? "active" : ""} onClick={onHistory}>History</button>
      </nav>
      <div className="top-actions">
        <button className="quiet" onClick={onDemo}>Try demo ↗</button>
        <button className="btn dark small" onClick={onBrowse}><Icon name="upload" size={15} /> <span>New upload</span></button>
      </div>
    </header>
  );
}

export function Footer() {
  return <div className="footnote">Scan In / Channel360 · Originals are stored exactly as uploaded · Every saved version keeps its own format and history</div>;
}

export { Icon };

export function Main({ className = "main", children }: { className?: string; children: ReactNode }) {
  return <main className={className}>{children}</main>;
}

export function FolderRows({ folders, onOpen }: { folders: Folder[]; onOpen: (id: string) => void }) {
  if (!folders.length) return <div className="empty"><h3>No folders found</h3><p>Try another search or scan a new file.</p></div>;
  return <>{folders.map((f) => {
    const newest = f.versions[f.versions.length - 1];
    return (
      <button key={f.document_id} className="history-item folder-row" onClick={() => onOpen(f.document_id)}>
        <span className="folder-icon"><Icon name="layers" size={23} /></span>
        <span className="folder-info">
          <strong>{folderName(f.name)}</strong>
          <small>{typeLabel(f.name)} original · {f.versions.length} version{f.versions.length === 1 ? "" : "s"} · Last change {humanDate(newest.created_at)}</small>
          <span className="folder-crumbs">{f.versions.slice(-5).map((v) => <em key={v.version_id} className={`mini-version ${v.kind === "original" ? "source" : ""}`}>v{v.number}</em>)}</span>
        </span>
        <span className="folder-chevron">Open folder <Icon name="arrow" size={15} /></span>
      </button>
    );
  })}</>;
}

export { fmtSize };
