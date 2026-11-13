import { Footer, FolderRows, Icon } from "./Shell";
import { folderName, humanDate, size, typeLabel, type Folder } from "../lib/api";

export function History({ folders, search, onSearch, onBrowse, onOpenFolder }: {
  folders: Folder[]; search: string; onSearch: (q: string) => void; onBrowse: () => void; onOpenFolder: (id: string) => void;
}) {
  return (
    <main className="main library">
      <div className="library-top">
        <div>
          <div className="kicker">DOCUMENT HISTORY / FOLDERS</div>
          <h1>Your document library<span style={{ color: "var(--coral-ink)" }}>.</span></h1>
          <p>One source. One folder. Every edit and subsequent version, together.</p>
        </div>
        <button className="btn dark" onClick={onBrowse}><Icon name="upload" size={16} /> Scan another file</button>
      </div>
      <div className="library-grid">
        <section className="history-list">
          <div className="history-toolbar">
            <input className="search" id="history-search" aria-label="Search document folders" placeholder="Find a document folder or version…" value={search} onChange={(e) => onSearch(e.target.value)} />
            <span className="history-count">{folders.length} folder{folders.length === 1 ? "" : "s"}</span>
          </div>
          <div id="history-results"><FolderRows folders={folders} onOpen={onOpenFolder} /></div>
        </section>
        <aside className="history-aside">
          <div className="kicker">ONE HOME FOR EVERY DOCUMENT</div>
          <h3>A folder that remembers every version.</h3>
          <p>Your first upload creates its document folder. Each edit lives inside it—without ever replacing your original.</p>
          <div className="history-step"><span>01</span><div><b>Original · v1</b>The exact file as uploaded</div></div>
          <div className="history-step"><span>02</span><div><b>Working version · v2</b>Edits saved as a separate derivative</div></div>
          <div className="history-step"><span>03</span><div><b>Continue · v3, v4…</b>Open any version and evolve it</div></div>
        </aside>
      </div>
      <Footer />
    </main>
  );
}

export function FolderPage({ folder, onHistory, onOpen, onCompare }: { folder: Folder | undefined; onHistory: () => void; onOpen: (documentId: string, versionId: string) => void; onCompare: (documentId: string, baseId: string, targetId: string) => void }) {
  if (!folder) return <History folders={[]} search="" onSearch={() => {}} onBrowse={() => {}} onOpenFolder={() => {}} />;
  const family = folder.versions;
  const latest = family[family.length - 1];
  return (
    <main className="main library folder-page">
      <div className="crumb"><button onClick={onHistory}>Document history</button><span>/</span><strong>{folderName(folder.name)}</strong></div>
      <section className="folder-hero">
        <div className="folder-header-icon"><Icon name="layers" size={33} /></div>
        <div className="folder-header-copy">
          <div className="kicker">DOCUMENT FAMILY</div>
          <h1>{folderName(folder.name)}</h1>
          <p>{family.length} connected version{family.length === 1 ? "" : "s"} · Source uploaded {humanDate(folder.created_at)} · {typeLabel(folder.name)}</p>
        </div>
        <button className="btn outline" onClick={() => onOpen(folder.document_id, latest.version_id)}>Open latest <Icon name="arrow" size={15} /></button>
      </section>
      <div className="folder-intro">
        <span><Icon name="shield" size={17} /> Source preserved from day one</span>
        <span><Icon name="layers" size={17} /> {family.length} visible versions</span>
        <span><Icon name="edit" size={17} /> Changes become new versions</span>
      </div>
      <section className="folder-versions">
        <div className="folder-versions-title"><h2>Version timeline</h2><p>Choose any version to inspect it, read its chunks or create the next working copy.</p></div>
        {family.map((v, i) => (
          <div className="version-item" key={v.version_id}>
            <div className={`timeline-marker ${i === 0 ? "source" : ""}`}>{i === 0 ? <Icon name="shield" size={19} /> : String(i + 1).padStart(2, "0")}</div>
            <div className="version-card">
              <div className="version-card-main">
                <div className="version-card-label"><strong>v{i + 1}</strong> <span className={`pill ${i === 0 ? "" : v.classification === "unsigned_working_copy" ? "wc" : "orange"}`}>{i === 0 ? "Original source" : v.classification === "unsigned_working_copy" ? "Unsigned working copy · not official" : "Saved"}</span>{v.outcome && i > 0 ? <span className="pill">{v.outcome}</span> : null}{i === family.length - 1 ? <span className="latest-label">Latest</span> : null}</div>
                <h3>{v.name}</h3>
                <p>{humanDate(v.created_at)} · {size(v.size_bytes)} {i > 0 ? `· Derived from v${v.parent_number ?? i}` : "· Uploaded file · Immutable"}</p>
              </div>
              <div className="version-actions">
                <button className={`btn ${i === family.length - 1 ? "dark" : "outline"} small`} onClick={() => onOpen(folder.document_id, v.version_id)}>Open v{i + 1} <Icon name="arrow" size={14} /></button>
                {v.parent_number ? <button className="btn outline small" onClick={() => onCompare(folder.document_id, family[v.parent_number! - 1].version_id, v.version_id)}>Compare with v{v.parent_number}</button> : null}
                {family.length > 1 ? (
                  <select className="compare-with" aria-label={`Compare v${i + 1} with another version`} value="" onChange={(e) => {
                    const other = family.find((x) => x.version_id === e.target.value); if (!other) return;
                    const [older, newer] = other.number < v.number ? [other, v] : [v, other];
                    onCompare(folder.document_id, older.version_id, newer.version_id);
                  }}>
                    <option value="">Compare with…</option>
                    {family.filter((x) => x.version_id !== v.version_id).map((x) => <option key={x.version_id} value={x.version_id}>v{x.number}{x.kind === "original" ? " · Original" : ""}</option>)}
                  </select>
                ) : null}
              </div>
            </div>
          </div>
        ))}
      </section>
      <Footer />
    </main>
  );
}
