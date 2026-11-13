import { useCallback, useEffect, useMemo, useState } from "react";
import { Icon } from "./Shell";
import {
  ApiError, cancelSheetIndex, compareVersions, fetchRenderBytes, fetchVersionFile, folderName, humanDate, size,
  type Change, type Comparison, type Folder, type VersionCard,
} from "../lib/api";
import { PageViewer, type Highlight } from "../components/PageViewer";
import { VirtualWorkbook } from "../components/VirtualWorkbook";
import { wordDiff, type Piece } from "../lib/textdiff";

type Props = {
  folder: Folder | undefined; baseId: string; targetId: string;
  onChange: (baseId: string, targetId: string) => void; onHistory: () => void; onFolder: () => void;
  onOpen: (versionId: string) => void; notify: (m: string, error?: boolean) => void;
};

const KIND_LABEL = { added: "Added", removed: "Removed", modified: "Modified" } as const;
const CATEGORY_LABEL: Record<string, string> = { text: "Text", value: "Cell value", formula: "Formula", format: "Formatting", structure: "Structure", image: "Image" };
export const CLASS_LABEL: Record<string, string> = { original: "Original source", derived: "Saved version", unsigned_working_copy: "Unsigned working copy · not official" };
const SIGNATURE_LABEL: Record<string, string> = {
  none: "No digital signature", signed: "Signature structure present · not validated", signature_range_mismatch: "Malformed signature · not trusted",
  modified_after_signing: "Changed after signing · not trusted", unsigned_signature_field: "Empty signature field",
};
const MODIFICATION: Record<string, string> = { pdf_text: "PDF text edit", native_release: "Native edit", pdf_working_copy: "Unsigned working copy created", text: "Text edit", rich: "Rich-text edit" };

function Pieces({ pieces }: { pieces: Piece[] }) {
  return <>{pieces.map((p, i) => p.kind === "same" ? <span key={i}>{p.text}</span> : <mark key={i} className={p.kind === "add" ? "diff-add" : "diff-del"}>{p.text}</mark>)}</>;
}

function ChangeRow({ change, active, onPick }: { change: Change; active: boolean; onPick: () => void }) {
  const diff = useMemo(() => (change.before != null && change.after != null && change.category !== "image" ? wordDiff(change.before, change.after) : null), [change]);
  return (
    <button className={`change-row ${change.kind} ${active ? "active" : ""}`} data-change={change.id} onClick={onPick} aria-current={active}>
      <span className="change-head"><b className={`change-kind ${change.kind}`}>{KIND_LABEL[change.kind]}</b><span className="change-cat">{CATEGORY_LABEL[change.category]}</span><span className="change-loc">{change.label}</span></span>
      {change.detail ? <span className="change-detail">{change.detail}</span> : null}
      {(change.before != null || change.after != null) && !change.detail ? (
        <span className="change-body">
          {change.before != null ? <span className="change-before"><i>Before</i> {diff ? <Pieces pieces={diff.before} /> : change.before || "(empty)"}</span> : null}
          {change.after != null ? <span className="change-after"><i>After</i> {diff ? <Pieces pieces={diff.after} /> : change.after || "(empty)"}</span> : null}
        </span>
      ) : null}
    </button>
  );
}

function VersionLine({ v, signature }: { v: VersionCard; signature?: string }) {
  return (
    <div className="audit-line">
      <b>v{v.number}</b>
      <span className={`pill ${v.classification === "unsigned_working_copy" ? "warn" : v.kind === "original" ? "" : "orange"}`}>{CLASS_LABEL[v.classification] ?? v.classification}</span>
      <span>{v.kind === "original" ? "Uploaded file · immutable" : `Saved from v${v.parent_number ?? "?"}`}</span>
      {v.outcome ? <span className="pill">{v.outcome}</span> : null}
      {signature ? <span className="pill">{SIGNATURE_LABEL[signature] ?? signature}</span> : null}
      <span>{v.modification ? `${MODIFICATION[v.modification] ?? v.modification}${v.edit_count ? ` (${v.edit_count})` : ""}` : "—"}</span>
      <span>{v.actor ? `by ${v.actor}` : "actor not recorded"}</span>
      <span>{humanDate(v.recorded_at ?? v.created_at)}</span>
      <span>{size(v.size_bytes)}</span>
    </div>
  );
}

function relationText(c: Comparison): string {
  const [b, t] = [c.base.number, c.target.number];
  const path = c.relation.path_numbers.map((n) => `v${n}`).join(" → ");
  switch (c.relation.type) {
    case "same": return `Both sides are v${b}.`;
    case "base_is_ancestor": return c.relation.path_numbers.length === 2 ? `v${t} was saved directly from v${b}.` : `v${t} descends from v${b} through ${path}. Changes shown are cumulative.`;
    case "target_is_ancestor": return `v${t} is an earlier ancestor of v${b} (${path}); the comparison runs backwards in time.`;
    case "branch": return `v${b} and v${t} are separate branches: ${path}. They are not in a straight line of ancestry.`;
    default: return `v${b} and v${t} are not directly related.`;
  }
}

export function ComparePage({ folder, baseId, targetId, onChange, onHistory, onFolder, onOpen, notify }: Props) {
  const documentId = folder?.document_id ?? "";
  const [cmp, setCmp] = useState<Comparison | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<"changes" | "all">("all");
  const [filter, setFilter] = useState<string>("all");
  const [offset, setOffset] = useState(0);                         // first change of the page being shown
  const [current, setCurrent] = useState(0);                       // index within the page
  const [progress, setProgress] = useState<Comparison | null>(null);   // set while workbooks are being prepared for a complete comparison
  const [cancelled, setCancelled] = useState(false);
  const family = folder?.versions ?? [];
  const isSheet = family.length > 0 && /\.xlsx$/i.test(folder?.name ?? "");
  const PAGE = isSheet ? 500 : 5000;

  // a different pair of versions starts over
  useEffect(() => { setCmp(null); setError(null); setOffset(0); setCurrent(0); setProgress(null); setCancelled(false); }, [documentId, baseId, targetId, filter]);

  useEffect(() => {
    if (!documentId || cancelled) return;
    let live = true; let timer: number | undefined;
    const kind = ["added", "removed", "modified"].includes(filter) ? filter : undefined;
    const category = filter !== "all" && !kind ? filter : undefined;
    const run = () => compareVersions(documentId, baseId, targetId, { offset, limit: PAGE, kind, category, wait: 6 })
      .then((c) => {
        if (!live) return;
        if (c.state === "building") { setProgress(c); timer = window.setTimeout(run, 1200); return; }
        setProgress(null); setCmp(c);
      })
      .catch((e) => { if (live) { const m = e instanceof ApiError ? e.message : "The comparison could not be made."; setError(m); notify(m, true); } });
    void run();
    return () => { live = false; window.clearTimeout(timer); };
  }, [documentId, baseId, targetId, filter, offset, notify, cancelled, PAGE]);

  const visible = cmp?.changes ?? [];
  const total = cmp?.page?.total ?? visible.length;
  const globalIndex = offset + Math.min(current, Math.max(0, visible.length - 1));
  const active = visible[Math.min(current, Math.max(0, visible.length - 1))] ?? null;
  const go = useCallback((delta: number) => {
    if (!total) return;
    const next = (globalIndex + delta + total) % total;
    const pageStart = Math.floor(next / PAGE) * PAGE;
    if (pageStart !== offset) { setOffset(pageStart); setCmp(null); }
    setCurrent(next - pageStart);
  }, [total, globalIndex, offset, PAGE]);
  const cancelPreparing = () => { setCancelled(true); setProgress(null); void Promise.allSettled([cancelSheetIndex(documentId, baseId), cancelSheetIndex(documentId, targetId)]); };

  const highlights = useMemo(() => {
    const base: Highlight[] = [], target: Highlight[] = [];
    for (const c of visible) {
      if (c.category !== "text" || !c.page) continue;
      if (c.base_bbox && c.kind !== "added") base.push({ id: c.id, page: c.page, bbox: c.base_bbox, kind: c.kind === "removed" ? "del" : "mod", label: c.label });
      if (c.target_bbox && c.kind !== "removed") target.push({ id: c.id, page: c.page, bbox: c.target_bbox, kind: c.kind === "added" ? "add" : "mod", label: c.label });
    }
    return { base, target };
  }, [visible]);
  const changedPages = useMemo(() => Array.from(new Set(visible.map((c) => c.page).filter((p): p is number => !!p))).sort((a, b) => a - b), [visible]);

  const focus = useMemo(() => (active?.location.sheet && active.location.cell ? { sheet: active.location.sheet, cell: active.location.cell, nonce: globalIndex + 1 } : null), [active, globalIndex]);

  if (!folder) return <main className="main"><div className="empty"><h3>Loading…</h3></div></main>;
  const numberOf = (id: string) => family.find((v) => v.version_id === id)?.number ?? 0;
  const numberOfId = numberOf;
  const label = (v: Folder["versions"][number]) => `v${v.number} · ${v.kind === "original" ? "Original" : `from v${v.parent_number ?? "?"}`}`;
  const counts = cmp?.summary;
  const categories = Object.keys(cmp?.categories ?? {});

  return (
    <main className="main compare-page" data-testid="compare-page">
      <div className="crumb"><button onClick={onHistory}>Document history</button><span>/</span><button onClick={onFolder}>{folderName(folder.name)}</button><span>/</span><strong>Compare versions</strong></div>
      <section className="compare-head">
        <div>
          <div className="kicker">COMPARE VERSIONS</div>
          <h1>What changed in {folderName(folder.name)}</h1>
        </div>
        <div className="compare-picker" role="group" aria-label="Choose versions to compare">
          <label>Base (older)<select aria-label="Base version" value={baseId} onChange={(e) => onChange(e.target.value, targetId)}>{family.map((v) => <option key={v.version_id} value={v.version_id}>{label(v)}</option>)}</select></label>
          <button className="btn outline small" aria-label="Swap versions" onClick={() => onChange(targetId, baseId)}>⇄</button>
          <label>Compare with (newer)<select aria-label="Target version" value={targetId} onChange={(e) => onChange(baseId, e.target.value)}>{family.map((v) => <option key={v.version_id} value={v.version_id}>{label(v)}</option>)}</select></label>
          <button className="btn outline small" onClick={() => { const t = family.find((v) => v.version_id === targetId); const parent = t?.parent_number ? family.find((v) => v.number === t.parent_number) : null; if (parent) onChange(parent.version_id, targetId); }}
            disabled={!family.find((v) => v.version_id === targetId)?.parent_number}>Compare with parent</button>
        </div>
      </section>
      {error ? <div className="empty"><h3>Comparison unavailable</h3><p>{error}</p></div> : !cmp ? cancelled ? <div className="viewer-note" role="status">The comparison was cancelled. Nothing was changed.</div>
        : progress ? (
          <div className="compare-progress viewer-note" role="status" data-testid="compare-progress">
            <b>Preparing both workbooks for a complete comparison…</b>
            <span>Version {numberOfId(baseId)}: {progress.progress?.base ?? "…"} · Version {numberOfId(targetId)}: {progress.progress?.target ?? "…"}. Large workbooks are read once, then compared cell by cell; no partial result is shown.</span>
            <button className="btn outline small" onClick={cancelPreparing}>Cancel</button>
          </div>
        ) : <p className="stage-note">Comparing the saved files…</p> : (
        <>
          <section className="compare-audit" aria-label="Version lineage and audit details">
            <p className="relation" data-testid="relation">{relationText(cmp)}</p>
            <div className="audit-lines">
              {cmp.path.length > 1 || cmp.relation.type === "same" ? cmp.path.map((v) => <VersionLine key={v.version_id} v={v} signature={cmp.format === "pdf" ? (v.version_id === cmp.base.version_id ? cmp.signature?.base.status : v.version_id === cmp.target.version_id ? cmp.signature?.target.status : undefined) : undefined} />)
                : [cmp.base, cmp.target].map((v, i) => <VersionLine key={v.version_id} v={v} signature={cmp.format === "pdf" ? (i === 0 ? cmp.signature?.base.status : cmp.signature?.target.status) : undefined} />)}
            </div>
            <p className="stage-note">Actor and time come from the audit trail only; "not recorded" means the trail has no entry for that version.</p>
          </section>
          <section className="compare-summary">
            <div className="summary-pills" data-testid="change-summary">
              <button className={`pill sum-add ${filter === "added" ? "on" : ""}`} onClick={() => setFilter(filter === "added" ? "all" : "added")}>+{counts?.added} added</button>
              <button className={`pill sum-del ${filter === "removed" ? "on" : ""}`} onClick={() => setFilter(filter === "removed" ? "all" : "removed")}>−{counts?.removed} removed</button>
              <button className={`pill sum-mod ${filter === "modified" ? "on" : ""}`} onClick={() => setFilter(filter === "modified" ? "all" : "modified")}>~{counts?.modified} modified</button>
              {categories.length > 1 ? categories.map((c) => <button key={c} className={`pill ${filter === c ? "on" : ""}`} onClick={() => setFilter(filter === c ? "all" : c)}>{CATEGORY_LABEL[c]}</button>) : null}
              {cmp.complete === false ? <span className="pill sum-del" data-testid="completeness">PARTIAL — not every change is listed</span>
                : <span className="pill sum-add" data-testid="completeness">COMPLETE{cmp.examined_cells ? ` · ${cmp.examined_cells.toLocaleString()} cells examined` : ""}</span>}
            </div>
            <div className="compare-nav">
              <div className="seg" role="group" aria-label="Content to show">
                <button className={mode === "changes" ? "active" : ""} onClick={() => setMode("changes")}>Changed content</button>
                <button className={mode === "all" ? "active" : ""} onClick={() => setMode("all")}>Show all content</button>
              </div>
              <button className="btn outline small" aria-label="Previous change" disabled={!total} onClick={() => go(-1)}>‹ Previous</button>
              <span className="stage-note" aria-live="polite">{total ? `Change ${globalIndex + 1} of ${total.toLocaleString()}` : "No changes"}</span>
              <button className="btn dark small" aria-label="Next change" disabled={!total} onClick={() => go(1)}>Next ›</button>
            </div>
          </section>
          {cmp.note ? <div className="source-banner"><Icon name="shield" size={14} /> <span>{cmp.note}</span></div> : null}
          <div className={`compare-body ${cmp.format}`}>
            <aside className="change-list" aria-label="Changes">
              {visible.length ? visible.map((c, i) => <ChangeRow key={c.id} change={c} active={i === current} onPick={() => setCurrent(i)} />) : <p className="stage-note" style={{ padding: 14 }}>{cmp.summary.total ? "No changes match this filter." : "No differences found in the compared content."}</p>}
              {total > PAGE ? (
                <div className="list-pager" data-testid="change-pager">
                  <button className="btn outline small" disabled={offset === 0} onClick={() => { setOffset(Math.max(0, offset - PAGE)); setCurrent(0); setCmp(null); }}>‹ Earlier</button>
                  <span className="stage-note">{(offset + 1).toLocaleString()}–{Math.min(offset + PAGE, total).toLocaleString()} of {total.toLocaleString()}</span>
                  <button className="btn outline small" disabled={offset + PAGE >= total} onClick={() => { setOffset(offset + PAGE); setCurrent(0); setCmp(null); }}>Later ›</button>
                </div>
              ) : null}
              <div className="limits">{cmp.limits.map((l) => <p key={l} className="stage-note">{l}</p>)}</div>
            </aside>
            <div className="compare-visual">
              {cmp.format === "pdf" ? (
                <div className="side-by-side">
                  <PageViewer key={`b-${baseId}`} title={`v${cmp.base.number} pages`} heading={`v${cmp.base.number} · base`} testId="viewer-base" fetchBytes={async () => (await fetchVersionFile(documentId, baseId)).arrayBuffer()}
                    highlights={highlights.base} activeHighlight={active?.id} onlyPages={mode === "changes" ? changedPages : null} />
                  <PageViewer key={`t-${targetId}`} title={`v${cmp.target.number} pages`} heading={`v${cmp.target.number} · target`} testId="viewer-target" fetchBytes={async () => (await fetchVersionFile(documentId, targetId)).arrayBuffer()}
                    highlights={highlights.target} activeHighlight={active?.id} onlyPages={mode === "changes" ? changedPages : null} />
                </div>
              ) : null}
              {cmp.format === "xlsx" && mode === "all" ? (
                <div className="side-by-side">
                  <div><div className="viewer-heading">v{cmp.base.number} · base</div><VirtualWorkbook key={`b-${baseId}`} documentId={documentId} versionId={baseId} readOnly compare={{ base: baseId, target: targetId, side: "base" }} focus={focus} label="Base workbook" testId="sheet-base" /></div>
                  <div><div className="viewer-heading">v{cmp.target.number} · target</div><VirtualWorkbook key={`t-${targetId}`} documentId={documentId} versionId={targetId} readOnly compare={{ base: baseId, target: targetId, side: "target" }} focus={focus} label="Target workbook" testId="sheet-target" /></div>
                </div>
              ) : null}
              {cmp.format === "docx" && mode === "all" ? (
                <div className="side-by-side">
                  <PageViewer key={`b-${baseId}`} title={`v${cmp.base.number} pages`} heading={`v${cmp.base.number} · base`} testId="viewer-base" fetchBytes={() => fetchRenderBytes(documentId, baseId)} note="Page-accurate preview. Text and formatting changes are listed on the left; a text list is not a full visual comparison." />
                  <PageViewer key={`t-${targetId}`} title={`v${cmp.target.number} pages`} heading={`v${cmp.target.number} · target`} testId="viewer-target" fetchBytes={() => fetchRenderBytes(documentId, targetId)} />
                </div>
              ) : null}
              <div className="compare-actions">
                <button className="btn outline small" onClick={() => onOpen(baseId)}>Open v{numberOf(baseId)} <Icon name="arrow" size={13} /></button>
                <button className="btn outline small" onClick={() => onOpen(targetId)}>Open v{numberOf(targetId)} <Icon name="arrow" size={13} /></button>
              </div>
            </div>
          </div>
        </>
      )}
    </main>
  );
}
