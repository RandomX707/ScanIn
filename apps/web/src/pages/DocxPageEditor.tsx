import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, fetchDocxDraft, fetchRenderBytes, type DocxBlock, type Preview, type SaveBody } from "../lib/api";
import { locateBlocks, readPageText, type Located } from "../lib/docxLocate";
import { PageViewer, type Highlight } from "../components/PageViewer";

type Props = { documentId: string; versionId: string; preview: Preview; onBody: (body: SaveBody | null) => void; notify: (message: string, error?: boolean) => void };

const describeFormat = (b: DocxBlock) => {
  const p = b.props ?? {};
  const parts = [p.font, p.size ? `${p.size} pt` : null, p.bold ? "bold" : null, p.italic ? "italic" : null, p.underline ? "underlined" : null,
    p.color && p.color !== "auto" ? `colour #${p.color}` : null, p.align ? `${p.align}-aligned` : null].filter(Boolean);
  return parts.length ? parts.join(" · ") : "Inherits the document's style";
};
const place = (b: DocxBlock) => b.table ? `table ${b.table.index + 1}, row ${b.table.row + 1}, column ${b.table.col + 1}` : b.kind === "paragraph" ? (b.heading ? "heading" : "paragraph") : b.kind;

/** The paginated LibreOffice rendering is the editing surface: what you see here is what Original shows. Edits are made to native text targets
 *  chosen on the page (when the location is verified) or from the synchronized list, and the proposed result is rendered before anything is saved. */
export function DocxPageEditor({ documentId, versionId, preview, onBody, notify }: Props) {
  const blocks = useMemo(() => preview.blocks ?? [], [preview]);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [locations, setLocations] = useState<Record<string, Located>>({});
  const [locating, setLocating] = useState(true);
  const [view, setView] = useState<"original" | "draft">("original");
  const [draft, setDraft] = useState<{ bytes: ArrayBuffer; key: string; nonce: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [pages, setPages] = useState<{ original: number; draft: number }>({ original: 0, draft: 0 });
  const [query, setQuery] = useState("");
  const [onlyEditable, setOnlyEditable] = useState(true);
  const nonce = useRef(0);

  const changes = useMemo(() => blocks.filter((b) => b.e && b.target_id && b.id in edits)
    .map((b) => ({ target_id: b.target_id!, fingerprint: b.fp!, value: edits[b.id] })), [blocks, edits]);
  const changesKey = JSON.stringify(changes);
  useEffect(() => { onBody(changes.length ? { changes } : null); },
  // eslint-disable-next-line react-hooks/exhaustive-deps
  [changesKey]);

  useEffect(() => {
    let live = true;
    (async () => {
      try { const found = locateBlocks(blocks, await readPageText(await fetchRenderBytes(documentId, versionId))); if (live) setLocations(found); }
      catch { /* the list still works; locations simply stay unverified */ }
      finally { if (live) setLocating(false); }
    })();
    return () => { live = false; };
  }, [blocks, documentId, versionId]);

  const stale = draft !== null && draft.key !== changesKey;
  const highlights: Highlight[] = useMemo(() => blocks.filter((b) => b.e && locations[b.id]).map((b) => ({
    id: b.id, page: locations[b.id].page, bbox: locations[b.id].bbox, kind: "edit" as const, label: `Edit: ${b.text.slice(0, 40)}` })), [blocks, locations]);
  const chosen = blocks.find((b) => b.id === selected) ?? null;
  const shown = blocks.filter((b) => (!onlyEditable || b.e) && (!query || b.text.toLowerCase().includes(query.toLowerCase())));

  const set = (b: DocxBlock, value: string) => {
    const clean = value.replace(/[\r\n]+/g, " ");
    setEdits((current) => { const next = { ...current }; if (clean === b.text) delete next[b.id]; else next[b.id] = clean; return next; });
    setProblem(null);
  };
  const revert = (b: DocxBlock) => set(b, b.text);
  const discardAll = () => { setEdits({}); setDraft(null); setView("original"); setProblem(null); };

  const requestDraft = useCallback(async () => {
    if (!changes.length || busy) return;
    setBusy(true); setProblem(null);
    try {
      const bytes = await fetchDocxDraft(documentId, versionId, changes);
      nonce.current += 1; setDraft({ bytes, key: changesKey, nonce: nonce.current }); setView("draft");
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "The preview of your changes could not be produced.";
      setProblem(message); notify(message, true);
    } finally { setBusy(false); }
  }, [changes, changesKey, busy, documentId, versionId, notify]);

  const reflow = view === "draft" && draft && !stale && pages.original && pages.draft && pages.original !== pages.draft;
  return (
    <div className="docx-page-editor" data-testid="docx-page-editor">
      <div>
        <div className="dpe-tabs" role="tablist" aria-label="Page view" style={{ marginBottom: 8 }}>
          <button role="tab" aria-selected={view === "original"} className={`btn small ${view === "original" ? "dark" : "outline"}`} onClick={() => setView("original")}>This version</button>
          <button role="tab" aria-selected={view === "draft"} data-testid="draft-tab" className={`btn small ${view === "draft" ? "dark" : "outline"}`} disabled={!draft} onClick={() => setView("draft")}>
            Preview of your changes{stale ? " (out of date)" : ""}</button>
        </div>
        {view === "original" || !draft
          ? <PageViewer key="original" title={`v pages`} testId="docx-edit-viewer" note={preview.render?.note} fetchBytes={() => fetchRenderBytes(documentId, versionId)}
              highlights={highlights} activeHighlight={selected && locations[selected] ? selected : null} onSelectHighlight={setSelected}
              onPages={(p) => setPages((x) => ({ ...x, original: p.length }))} />
          : <PageViewer key={`draft-${draft.nonce}`} title="Preview of your changes" testId="docx-draft-viewer" note={stale ? "These pages show an earlier set of changes. Preview again to refresh." : "Drawn by LibreOffice from your proposed edits. Nothing is saved yet."}
              fetchBytes={async () => draft.bytes.slice(0)} onPages={(p) => setPages((x) => ({ ...x, draft: p.length }))} />}
        {reflow ? <div className="dpe-warn" role="status">This change moves content: the document now has {pages.draft} pages instead of {pages.original}. Review the pages before saving.</div> : null}
        {locating ? <p className="stage-note">Locating editable text on the pages…</p>
          : <p className="stage-note" data-testid="locate-summary">{highlights.length} of {blocks.filter((b) => b.e).length} editable texts have a verified position on the page; select the rest from the list.</p>}
      </div>
      <aside className="dpe-panel" aria-label="Edit selected text">
        <div className="dpe-edit" data-testid="edit-panel">
          {chosen ? (
            <>
              <strong>{place(chosen)}</strong>
              <div className="dpe-fmt">{describeFormat(chosen)}</div>
              <label>Original</label>
              <div className="dpe-original" data-testid="original-value">{chosen.text}</div>
              {chosen.e ? (
                <>
                  <label htmlFor="dpe-new">Replacement</label>
                  <textarea id="dpe-new" data-testid="replacement" value={chosen.id in edits ? edits[chosen.id] : chosen.text} onChange={(e) => set(chosen, e.target.value)} spellCheck />
                  <div className="dpe-actions"><button className="btn outline small" onClick={() => revert(chosen)} disabled={!(chosen.id in edits)}>Revert this text</button>
                    <span className="dpe-fmt">{locations[chosen.id] ? `Page ${locations[chosen.id].page}` : "Position on the page is not verified"}</span></div>
                </>
              ) : <p className="dpe-fmt" data-testid="readonly-reason">Read-only: {chosen.r ?? "this text can't be changed here."}</p>}
            </>
          ) : <p className="stage-note">Select highlighted text on the page, or choose it from the list.</p>}
        </div>
        {problem ? <div className="dpe-error" role="alert" data-testid="draft-error">{problem}</div> : null}
        <div className="dpe-actions">
          <button className="btn dark small" data-testid="preview-changes" disabled={!changes.length || busy} onClick={requestDraft}>{busy ? "Rendering…" : `Preview changes${changes.length ? ` (${changes.length})` : ""}`}</button>
          <button className="btn outline small" disabled={!changes.length} onClick={discardAll}>Discard changes</button>
        </div>
        <div>
          <input aria-label="Search document text" placeholder="Search text" value={query} onChange={(e) => setQuery(e.target.value)} style={{ width: "100%", marginBottom: 6 }} />
          <label className="dpe-fmt"><input type="checkbox" checked={onlyEditable} onChange={(e) => setOnlyEditable(e.target.checked)} /> Editable text only</label>
        </div>
        <ul className="dpe-list" data-testid="target-list">
          {shown.slice(0, 400).map((b) => (
            <li key={b.id}>
              <button aria-current={selected === b.id} data-changed={b.id in edits} onClick={() => { setSelected(b.id); if (view === "draft") setView("original"); }}>
                <span>{(b.id in edits ? edits[b.id] : b.text).slice(0, 70) || "(empty)"}</span>
                <span className={`where ${locations[b.id] ? "" : "none"}`}>{locations[b.id] ? `p.${locations[b.id].page}` : b.e ? "list" : "locked"}</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>
    </div>
  );
}
