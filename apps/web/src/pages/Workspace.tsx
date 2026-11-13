import { useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "./Shell";
import {
  ApiError, ext, fetchRenderBytes, fetchVersionFile, folderName, getChunks, getPreview, humanDate, saveBlob, size, typeLabel,
  type Chunks, type Folder, type FolderVersion, type Preview,
} from "../lib/api";
import { EditsTab } from "./Edits";
import { SheetGrid } from "./SheetGrid";
import { PageViewer } from "../components/PageViewer";

const SOURCE_TEXT = ["txt", "text", "md", "markdown", "csv", "json", "xml", "rtf", "html", "htm"];
const NATIVE = ["pdf", "png", "jpg", "jpeg", "gif", "webp", "bmp"];
type Tab = "original" | "chunks" | "edits";

export type WorkspaceProps = {
  documentId: string; versionId: string; folders: Folder[];
  onHome: () => void; onHistory: () => void; onFolder: (id: string) => void;
  onOpen: (documentId: string, versionId: string, tab?: "original" | "chunks" | "edits") => void; onBrowse: () => void;
  initialTab?: "original" | "chunks" | "edits";
  notify: (message: string, error?: boolean) => void; refresh: () => Promise<void>;
};

function statusFor(version: FolderVersion, name: string, chunks: Chunks | null): string {
  if (version.kind === "derived") return "Saved";
  const e = ext(name);
  if (chunks && !chunks.text_available) {
    if (e === "pdf") return "Scanned PDF · OCR needed";
    if (["png", "jpg", "jpeg", "gif", "webp", "bmp"].includes(e)) return "Native image · no OCR";
  }
  if (e === "docx") return "Page preview · layout rendered locally";
  if (["xlsx", "xls", "pptx"].includes(e)) return "Extracted · layout approximate";
  return "Parsed";
}

export function Workspace(props: WorkspaceProps) {
  const { documentId, versionId, folders, notify } = props;
  const folder = folders.find((f) => f.document_id === documentId);
  const [tab, setTab] = useState<Tab>(props.initialTab ?? "original");
  const [selected, setSelected] = useState(0);
  const [chunks, setChunks] = useState<Chunks | null>(null);
  const [dirty, setDirty] = useState(false);
  const family = folder?.versions ?? [];
  const root = family[0];
  const version = family.find((v) => v.version_id === versionId);

  useEffect(() => {
    let live = true;
    getChunks(documentId, versionId).then((c) => { if (live) setChunks(c); })
      .catch((e) => { if (live) { setChunks({ sections: [], word_count: 0, text_available: false }); notify(e instanceof ApiError ? e.message : "The sections could not be read.", true); } });
    return () => { live = false; };
  }, [documentId, versionId, notify]);

  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => { if (dirty && tab === "edits") { e.preventDefault(); e.returnValue = ""; } };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty, tab]);

  if (!folder || !version || !root) return <main className="main workspace-shell"><div className="empty"><h3>Loading…</h3></div></main>;

  const sections = chunks?.sections ?? [];
  const index = Math.min(selected, Math.max(0, sections.length - 1));
  const name = version.name;
  const downloadSource = async () => {
    try { saveBlob(await fetchVersionFile(documentId, root.version_id), folder.name); }
    catch (e) { notify(e instanceof ApiError && e.status === 409 ? e.message : "The original could not be downloaded.", true); }
  };
  const [originalPage, setOriginalPage] = useState<number | undefined>(undefined);
  const chooseTab = (next: Tab) => { if (next !== "original") setOriginalPage(undefined); setTab(next); };
  const select = (i: number) => { setSelected(i); setTab("chunks"); };

  return (
    <main className="main workspace-shell">
      <div className="crumb">
        <button onClick={props.onHome}>Scan In</button><span>/</span>
        <button onClick={props.onHistory}>History</button><span>/</span>
        <button onClick={() => props.onFolder(documentId)}>{folderName(folder.name)}</button><span>/</span>
        <strong>v{version.number}</strong>
      </div>
      {folder.demo ? (
        <div className="sample-ribbon">
          <span><b>SYNTHETIC SAMPLE</b> · This document is demonstration data. Try the real source download and version controls.</span>
          <button className="btn outline small" onClick={props.onBrowse}>Upload your own</button>
        </div>
      ) : null}
      <div className="workspace-head">
        <div className="file-main">
          <div className="file-mark">{typeLabel(name)}</div>
          <div>
            <h2>{name}</h2>
            <div className="file-meta">{humanDate(version.created_at)} · {size(version.size_bytes)} · <strong>{statusFor(version, folder.name, chunks)}</strong> · Version {version.number} of {family.length}</div>
            <div className="source-identity"><Icon name="shield" size={13} /> Original: <strong>{folder.name}</strong> · preserved as uploaded</div>
          </div>
        </div>
        <div className="workspace-buttons">
          <button className="btn outline small" onClick={() => chooseTab("original")}><Icon name="file" size={15} /> View original</button>
          <button className="btn outline small" onClick={downloadSource}><Icon name="download" size={15} /> Exact source</button>
          <button className="btn dark small" onClick={props.onBrowse}><Icon name="upload" size={15} /> New Scan</button>
        </div>
      </div>
      <div className="version-bar">
        <div className="version-label"><Icon name="layers" size={16} /> <span>DOCUMENT FOLDER</span> <strong>{folderName(folder.name)}</strong></div>
        <div className="version-pills">
          {family.map((v) => (
            <button key={v.version_id} className={`version-pill ${v.version_id === versionId ? "active" : ""}`} title={v.name}
              onClick={() => props.onOpen(documentId, v.version_id)}>v{v.number} {v.kind === "original" ? "· Source" : ""}</button>
          ))}
        </div>
        <button className="version-open" onClick={() => props.onFolder(documentId)}>All versions <Icon name="arrow" size={14} /></button>
      </div>
      <div className="three-col">
        <aside className="panel map-panel">
          <div className="panel-label">DOCUMENT MAP</div>
          <div className="map-count">{sections.length} {sections.length === 1 ? "section" : "sections"} · concise takeaways</div>
          {sections.length ? sections.map((c, i) => (
            <button key={c.id} className={`map-section ${index === i ? "active" : ""}`} onClick={() => select(i)}>
              <span className="index">{String(i + 1).padStart(2, "0")}</span><span className="map-title">{c.title}</span>
            </button>
          )) : <p className="stage-note" style={{ padding: "0 10px" }}>No extractable sections</p>}
          <div className="map-divider" />
          <div className="rail-tip"><b>Short by design</b>Chunks show the takeaway, not a copy of the document. The complete source lives under Original.</div>
        </aside>
        <section className="panel stage">
          <div className="tabs" role="tablist" aria-label="Document views">
            <button role="tab" aria-selected={tab === "original"} className={`tab ${tab === "original" ? "active" : ""}`} onClick={() => chooseTab("original")}><span className="tab-index">01</span> <Icon name="file" size={16} /> Original</button>
            <button role="tab" aria-selected={tab === "chunks"} className={`tab ${tab === "chunks" ? "active" : ""}`} onClick={() => chooseTab("chunks")}><span className="tab-index">02</span> <Icon name="layers" size={16} /> Chunks</button>
            <button role="tab" aria-selected={tab === "edits"} className={`tab ${tab === "edits" ? "active" : ""}`} onClick={() => chooseTab("edits")}><span className="tab-index">03</span> <Icon name="edit" size={16} /> Edits {dirty ? "•" : ""}</button>
          </div>
          <div className="stage-inside" id="stage-body">
            {tab === "original" && <OriginalTab documentId={documentId} root={root} folderName={folder.name} download={downloadSource} page={originalPage} />}
            {tab === "chunks" && <ChunksTab chunks={chunks} index={index} onSelect={setSelected} onOriginal={(page) => { setOriginalPage(page); chooseTab("original"); }} />}
            {tab === "edits" && (
              <EditsTab documentId={documentId} folderName={folder.name} version={version} family={family} chunksText={chunks} setDirty={setDirty}
                notify={notify} refresh={props.refresh} onSaved={(id) => props.onOpen(documentId, id, "edits")} download={async () => {
                  try { saveBlob(await fetchVersionFile(documentId, versionId), downloadName(folder.name, version.number)); }
                  catch (e) { notify(e instanceof ApiError && e.status === 409 ? e.message : "The file could not be exported.", true); }
                }} />
            )}
          </div>
        </section>
        <aside className="panel insight-panel">
          <div><div className="panel-label">Document pulse</div><div className="insight-number">{String(sections.length).padStart(2, "0")}</div><div className="insight-desc">readable sections found</div></div>
          <div className="insight-divider" />
          <div>
            <div className="panel-label">At a glance</div>
            <div className="insight-line"><span>Words</span><b>{(chunks?.word_count ?? 0).toLocaleString()}</b></div>
            <div className="insight-line"><span>Format</span><b>{typeLabel(name)}</b></div>
            <div className="insight-line"><span>File size</span><b>{size(version.size_bytes)}</b></div>
            <div className="insight-line"><span>Origin</span><b>{version.kind === "derived" ? "Saved version" : "Original upload"}</b></div>
            <div className="insight-line"><span>Uploaded</span><b>{humanDate(version.created_at)}</b></div>
          </div>
          <div className="signal-card"><b><Icon name="spark" size={15} /> A useful distinction</b>{sections.length > 0 ? "The section map is grounded in readable source text, not an invented table of contents." : "No text layer was extracted. OCR or an appropriate parser is needed."}</div>
          <button className="btn muted small insight-cta" onClick={() => props.onFolder(documentId)}><Icon name="clock" size={14} /> Open document folder <Icon name="arrow" size={13} /></button>
        </aside>
      </div>
      <div className="footnote">Scan In / Channel360 · Originals are stored exactly as uploaded · Every saved version keeps its own format and history</div>
    </main>
  );
}

function downloadName(original: string, number: number) {
  if (number === 1) return original;
  const dot = original.lastIndexOf(".");
  return dot > 0 ? `${original.slice(0, dot)}_v${number}${original.slice(dot)}` : `${original}_v${number}`;
}

function decode(buffer: ArrayBuffer): string {
  try { return new TextDecoder("utf-8", { fatal: true }).decode(buffer); }
  catch { return new TextDecoder("windows-1252").decode(buffer); }
}

export function ExtractedPreview({ preview }: { preview: Preview }) {
  if (preview.format === "docx") {
    const nodes: React.ReactNode[] = [];
    const blocks = preview.blocks ?? [];
    for (let i = 0; i < blocks.length; i++) {
      const b = blocks[i];
      if (b.table) {
        const table = b.table.index; const cells: typeof blocks = [];
        while (i < blocks.length && blocks[i].table?.index === table) cells.push(blocks[i++]);
        i--;
        const rows = new Map<number, typeof blocks>();
        for (const c of cells) rows.set(c.table!.row, [...(rows.get(c.table!.row) ?? []), c]);
        nodes.push(<table key={b.id}><tbody>{[...rows.entries()].sort((x, y) => x[0] - y[0]).map(([r, row]) => <tr key={r}>{row.map((c) => <td key={c.id}>{c.text}</td>)}</tr>)}</tbody></table>);
      } else if (b.heading) {
        const Tag = (`h${Math.min(3, b.heading + 1)}`) as "h2";
        nodes.push(<Tag key={b.id}>{b.text}</Tag>);
      } else nodes.push(<p key={b.id}>{b.text}</p>);
    }
    return <>{nodes.length ? nodes : <p>No extractable text in this file.</p>}</>;
  }
  if (preview.format === "xlsx" || preview.format === "csv") {
    return <>{(preview.sheets ?? []).map((s) => (
      <div key={s.name}>
        <h2>{s.name}</h2>
        {s.layout
          ? <div className="grid-editor no-toolbar"><SheetGrid sheet={s} styles={preview.styles ?? []} readOnly /></div>
          : <table><tbody>{s.rows.map((row, r) => <tr key={r}>{row.map((c, ci) => (r === 0 ? <th key={ci}>{c?.v ?? ""}</th> : <td key={ci}>{c?.v ?? ""}</td>))}</tr>)}</tbody></table>}
      </div>
    ))}</>;
  }
  if (preview.format === "pptx") {
    return <>{(preview.slides ?? []).map((s, i) => <div key={i}><h2>Slide {i + 1}</h2>{s.map((t, j) => <p key={j}>{t}</p>)}</div>)}</>;
  }
  return <p>No extractable text in this file.</p>;
}

function OriginalTab({ documentId, root, folderName: name, download, page }: { documentId: string; root: FolderVersion; folderName: string; download: () => void; page?: number }) {
  const e = ext(name);
  const native = NATIVE.includes(e);
  const sourceText = SOURCE_TEXT.includes(e);
  const [text, setText] = useState<string | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const urlRef = useRef<string | null>(null);
  const [renderFailed, setRenderFailed] = useState(false);

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        if (sourceText) { const blob = await fetchVersionFile(documentId, root.version_id); if (live) setText(decode(await blob.arrayBuffer())); }
        else if (native) { const blob = await fetchVersionFile(documentId, root.version_id); if (live) { urlRef.current = URL.createObjectURL(blob); setUrl(urlRef.current); } }
        else { const p = await getPreview(documentId, root.version_id); if (live) setPreview(p); }
      } catch { if (live) setText("Preview unavailable. Download the exact original file to inspect it."); }
    })();
    return () => { live = false; if (urlRef.current) { URL.revokeObjectURL(urlRef.current); urlRef.current = null; } };
  }, [documentId, root.version_id, native, sourceText]);

  const faithful = preview?.format === "docx" && preview.render?.available === true && !renderFailed;
  const note = faithful ? "Page-accurate preview rendered locally for viewing only. Your original DOCX is unchanged; editing still saves a native DOCX."
    : native ? "Native preview from the exact original file. No conversion or document rewriting."
    : sourceText ? "Displaying the actual source text from the original file, without the parser’s transformations."
      : "Exact native Office rendering is not supported here. This reading preview is reconstructed; use Download exact source to open the real file in its native application.";
  return (
    <>
      <div className="section-eyebrow">01 / ORIGINAL SOURCE</div>
      <div className="section-title"><h3>Your original document.</h3><span className="pill"><Icon name="shield" size={13} /> Source never overwritten</span></div>
      <div className="source-banner"><Icon name="shield" size={14} /> <span>{note}</span></div>
      <div className="reader-toolbar">
        <span className="stage-note">{name} · {size(root.size_bytes)} · {faithful ? "Page-accurate preview" : native ? "Native file preview" : sourceText ? "Exact source text" : "Office reading preview"}</span>
        <button className="btn outline small" onClick={download}><Icon name="download" size={14} /> Download exact source</button>
      </div>
      <div className={`source-paper ${native ? "binary-original" : ""} ${sourceText ? "raw-original" : ""}`} id="source-view">
        {native ? (url
          ? (e === "pdf" ? <iframe key={page ?? 0} src={page ? `${url}#page=${page}` : url} title="Original PDF · native document viewer" className="native-pdf-frame" /> : <img src={url} alt="Exact original uploaded image" className="native-original-image" />)
          : <div className="stage-note">Opening the original file…</div>)
          : sourceText ? <pre className="raw-source" aria-label="Exact original source text">{text ?? "Loading exact original source…"}</pre>
            : faithful ? <PageViewer title="Original document pages" fetchBytes={() => fetchRenderBytes(documentId, root.version_id).catch((e) => { setRenderFailed(true); throw e; })} note={preview?.render?.note} testId="docx-page-viewer" />
            : <>{preview?.format === "docx" ? <div className="extracted-banner"><Icon name="layers" size={14} /> {preview.render?.available === false ? "PAGE PREVIEW UNAVAILABLE" : "PAGE PREVIEW FAILED"} · SHOWING EXTRACTED TEXT, NOT THE ORIGINAL LAYOUT · DOWNLOAD EXACT FILE ABOVE</div> : null}<div className="extracted-banner"><Icon name="layers" size={14} /> EXTRACTED TEXT · NOT A NATIVE OFFICE RENDER · DOWNLOAD EXACT FILE ABOVE</div>{preview ? <ExtractedPreview preview={preview} /> : <p className="stage-note">{text ?? "Reading the file…"}</p>}</>}
      </div>
    </>
  );
}

function ChunksTab({ chunks, index, onSelect, onOriginal }: { chunks: Chunks | null; index: number; onSelect: (i: number) => void; onOriginal: (page?: number) => void }) {
  const sections = useMemo(() => chunks?.sections ?? [], [chunks]);
  if (!chunks) return <p className="stage-note">Building the document map…</p>;
  if (!sections.length) return (
    <>
      <div className="section-eyebrow">02 / DOCUMENT UNDERSTANDING</div>
      <h3>No readable text detected</h3>
      <p className="stage-note">This format requires OCR or a dedicated extraction adapter before a meaningful summary can be created. The original remains preserved.</p>
      <button className="btn outline" onClick={() => onOriginal()}>Open the original <Icon name="arrow" size={15} /></button>
    </>
  );
  const c = sections[index];
  return (
    <>
      <div className="section-eyebrow">02 / THE ESSENTIALS</div>
      <div className="section-title"><h3>Just what matters.</h3><span className="pill">{sections.length} section{sections.length === 1 ? "" : "s"}</span></div>
      <div className="reader-toolbar"><span className="stage-note">Sections found from the document's own headings and labels.</span><span className="pill orange">Rule-based</span></div>
      <article className="chunk-head">
        <div className="kicker">CHUNK {String(index + 1).padStart(2, "0")} / {String(sections.length).padStart(2, "0")}</div>
        <h3>{c.title}</h3>
        <p>{c.summary || "This section is present, but a reliable summary could not be extracted."}</p>
      </article>
      <section className="takeaways">
        <div className="section-eyebrow">KEY TAKEAWAYS</div>
        {c.points.length ? c.points.slice(0, c.source ? 6 : 3).map((x, i) => <div className="takeaway" key={i}><span>{String(i + 1).padStart(2, "0")}</span><p>{x}</p></div>) : <p className="stage-note">No high-confidence key statements detected.</p>}
      </section>
      <div className="source-link">
        <span><Icon name="shield" size={15} /> {c.source ? `Source: ${c.source.label} · ` : ""}{c.words.toLocaleString()} source words{c.source ? "" : " · condensed for quick reading"}</span>
        <button className="btn outline small" onClick={() => onOriginal(c.source?.kind === "pdf" ? c.source.pages?.[0] : undefined)}>{c.source?.kind === "pdf" && c.source.pages?.length ? `View on page ${c.source.pages[0]}` : "View complete original"} <Icon name="arrow" size={14} /></button>
      </div>
      <div className="chunk-pagination">
        <button className="btn outline small" disabled={index === 0} onClick={() => onSelect(index - 1)}>← Previous chunk</button>
        <span className="stage-note">{index + 1} of {sections.length}</span>
        <button className="btn dark small" disabled={index === sections.length - 1} onClick={() => onSelect(index + 1)}>Next chunk <Icon name="arrow" size={13} /></button>
      </div>
      <p className="download-note" style={{ marginTop: 16 }}>Sections and values are read from the document by fixed rules, not by AI, and each one points to where it was found. Source text stays in Original.</p>
    </>
  );
}
