import { useRef, useState } from "react";
import { Footer, Icon } from "./Shell";
import { folderName, type Folder } from "../lib/api";

const FORMAT_ORDER = ["pdf", "docx", "xlsx", "pptx", "png", "txt", "md", "csv", "json", "html", "xml"];
const CAPTION = (k: string) => (k === "pdf" || k === "png" ? "Native preview" : ["docx", "xlsx", "pptx"].includes(k) ? "Extracted preview" : "Text preview");

export function Home({ folders, onBrowse, onFiles, onFormat, onFolder, onHistory }: {
  folders: Folder[]; onBrowse: () => void; onFiles: (files: File[]) => void;
  onFormat: (key: string) => void; onFolder: (id: string) => void; onHistory: () => void;
}) {
  const [dragging, setDragging] = useState(false);
  const zone = useRef<HTMLDivElement>(null);
  return (
    <main className="main">
      <section className="hero">
        <div className="hero-copy">
          <div className="kicker">A more meaningful way to work with documents</div>
          <h1>Don't just open it.<br /><em>Understand it.</em></h1>
          <p>From a single upload to a complete understanding of your document. Read the source, explore intelligent sections, and make edits—without ever losing what came before.</p>
          <button className="btn dark" onClick={onBrowse}><Icon name="upload" size={18} /> Scan in a document <Icon name="arrow" size={17} /></button>
          <div className="hero-features">
            <span><b className="dot-tick">✓</b> One workspace</span>
            <span><b className="dot-tick">✓</b> Original stays untouched</span>
            <span><b className="dot-tick">✓</b> Traceable history</span>
          </div>
        </div>
        <div className="drop-card">
          <div ref={zone} className={`drop-inner ${dragging ? "dragging" : ""}`} id="dropzone" role="button" tabIndex={0}
            aria-label="Upload documents. Drag and drop or press Enter to browse."
            onClick={onBrowse}
            onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onBrowse(); } }}
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => { e.preventDefault(); e.stopPropagation(); setDragging(false); onFiles(Array.from(e.dataTransfer.files)); }}>
            <div className="upload-icon"><Icon name="file" size={37} /></div>
            <div className="kicker" style={{ justifyContent: "center", letterSpacing: ".13em", marginBottom: 12 }}>Start with a document</div>
            <h3>Drop your file here</h3>
            <p>Watch it become a navigable document map with its source, sections and editable working copy.</p>
            <button type="button" className="btn orange"><Icon name="upload" size={16} /> Choose files</button>
            <small>PDF · Word · Excel · PowerPoint · Images · Text · Data</small>
          </div>
        </div>
      </section>
      <section className="flow-section">
        <div className="flow-heading">
          <div><div className="kicker">One document / Three perspectives</div><h2>Everything you need, nothing you don't.</h2></div>
          <p>A document isn't a list of actions. It's one connected reading experience—across source, meaning and change.</p>
        </div>
        <div className="flows">
          <article className="flow-card"><span className="flow-no">01 / Preserve</span><span className="flow-arrow">↗</span><h3>Original</h3><p>Your immutable point of reference. No silent overwrites, no lost source.</p></article>
          <article className="flow-card"><span className="flow-no">02 / Navigate</span><span className="flow-arrow">↗</span><h3>Chunks</h3><p>An index-first reading map, or meaningful sections when no index exists.</p></article>
          <article className="flow-card"><span className="flow-no">03 / Transform</span><span className="flow-arrow">↗</span><h3>Edits</h3><p>Work on a separate copy, then save a linked derived document.</p></article>
        </div>
      </section>
      <section className="format-section">
        <div className="format-heading">
          <div><div className="kicker">FORMAT PLAYGROUND</div><h2>One canvas. Many formats.</h2><p>Open a real synthetic sample in any format to explore source, meaningful chunks and a working copy.</p></div>
          <span className="format-legend"><Icon name="shield" size={14} /> Originals are preserved</span>
        </div>
        <div className="format-list">
          {FORMAT_ORDER.map((k) => (
            <button key={k} className="format-tile" data-format-name={k} title={`Open a sample ${k.toUpperCase()}`} onClick={() => onFormat(k)}>
              <span className="format-symbol">{k.toUpperCase()}</span><span className="format-caption">{CAPTION(k)}</span><Icon name="arrow" size={14} />
            </button>
          ))}
        </div>
        <div className="format-disclaimer">DOCX, XLSX and PPTX are displayed as extracted content, not pixel-perfect native rendering. Saved versions keep the format of the original file.</div>
      </section>
      <div className="recent-row">
        <div className="kicker">Pick up where you left off</div>
        {folders.slice(0, 3).map((f) => (
          <button key={f.document_id} className="recent-chip" onClick={() => onFolder(f.document_id)}>
            <Icon name="layers" size={14} /><span>{folderName(f.name)}</span> <strong className="tiny-version">{f.versions.length} versions</strong> <Icon name="chev" size={13} />
          </button>
        ))}
        <button className="btn muted small" onClick={onHistory}>All documents <Icon name="arrow" size={13} /></button>
      </div>
      <Footer />
    </main>
  );
}
