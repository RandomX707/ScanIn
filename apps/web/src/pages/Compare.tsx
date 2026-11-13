import { useCallback, useEffect, useMemo, useState } from "react";
import { Icon } from "./Shell";
import {
  ApiError, compareVersions, fetchRenderBytes, fetchVersionFile, folderName, getPreview, humanDate, size,
  type Change, type Comparison, type Folder, type Preview, type VersionCard,
} from "../lib/api";
import { PageViewer, type Highlight } from "../components/PageViewer";
import { SheetWorkspace } from "../components/SheetWorkspace";
import type { CellMark } from "./SheetGrid";
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
  const [current, setCurrent] = useState(0);
  const [previews, setPreviews] = useState<{ base: Preview; target: Preview } | null>(null);
  const family = folder?.versions ?? [];

  useEffect(() => {
    if (!documentId) return;
    let live = true;
    setCmp(null); setError(null); setCurrent(0); setPreviews(null);
    compareVersions(documentId, baseId, targetId)
      .then((c) => { if (live) setCmp(c); })
      .catch((e) => { if (live) { const m = e instanceof ApiError ? e.message : "The comparison could not be made."; setError(m); notify(m, true); } });
    return () => { live = false; };
  }, [documentId, baseId, targetId, notify]);

  useEffect(() => {
    if (!cmp || cmp.format !== "xlsx") return;
    let live = true;
    Promise.all([getPreview(documentId, baseId), getPreview(documentId, targetId)]).then(([b, t]) => { if (live) setPreviews({ base: b, target: t }); }).catch(() => undefined);
    return () => { live = false; };
  }, [cmp, documentId, baseId, targetId]);

  const visible = useMemo(() => (cmp?.changes ?? []).filter((c) => filter === "all" || c.category === filter || c.kind === filter), [cmp, filter]);
  const active = visible[Math.min(current, Math.max(0, visible.length - 1))] ?? null;
  const go = useCallback((delta: number) => { if (visible.length) setCurrent((i) => (i + delta + visible.length) % visible.length); }, [visible.length]);
  useEffect(() => { setCurrent(0); }, [filter]);

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

  const marks = useMemo(() => {
    const base: Record<string, Record<string, CellMark>> = {}, target: Record<string, Record<string, CellMark>> = {};
    for (const c of visible) {
      const { sheet, cell } = c.location; if (!sheet || !cell) continue;
      if (c.kind !== "added") (base[sheet] ??= {})[cell] = c.kind === "removed" ? "del" : "mod";
      if (c.kind !== "removed") (target[sheet] ??= {})[cell] = c.kind === "added" ? "add" : "mod";
    }
    return { base, target };
  }, [visible]);
  const focus = useMemo(() => (active?.location.sheet && active.location.cell ? { sheet: active.location.sheet, cell: active.location.cell, nonce: current } : null), [active, current]);

  if (!folder) return <main className="main"><div className="empty"><h3>Loading…</h3></div></main>;
  const numberOf = (id: string) => family.find((v) => v.version_id === id)?.number ?? 0;
  const label = (v: Folder["versions"][number]) => `v${v.number} · ${v.kind === "original" ? "Original" : `from v${v.parent_number ?? "?"}`}`;
  const counts = cmp?.summary;
  const categories = Array.from(new Set((cmp?.changes ?? []).map((c) => c.category)));

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
      {error ? <div className="empty"><h3>Comparison unavailable</h3><p>{error}</p></div> : !cmp ? <p className="stage-note">Comparing the saved files…</p> : (
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
              {counts?.truncated ? <span className="stage-note">Showing the first {counts.total} changes.</span> : null}
            </div>
            <div className="compare-nav">
              <div className="seg" role="group" aria-label="Content to show">
                <button className={mode === "changes" ? "active" : ""} onClick={() => setMode("changes")}>Changed content</button>
                <button className={mode === "all" ? "active" : ""} onClick={() => setMode("all")}>Show all content</button>
              </div>
              <button className="btn outline small" aria-label="Previous change" disabled={!visible.length} onClick={() => go(-1)}>‹ Previous</button>
              <span className="stage-note" aria-live="polite">{visible.length ? `Change ${Math.min(current, visible.length - 1) + 1} of ${visible.length}` : "No changes"}</span>
              <button className="btn dark small" aria-label="Next change" disabled={!visible.length} onClick={() => go(1)}>Next ›</button>
            </div>
          </section>
          {cmp.note ? <div className="source-banner"><Icon name="shield" size={14} /> <span>{cmp.note}</span></div> : null}
          <div className={`compare-body ${cmp.format}`}>
            <aside className="change-list" aria-label="Changes">
              {visible.length ? visible.map((c, i) => <ChangeRow key={c.id} change={c} active={i === current} onPick={() => setCurrent(i)} />) : <p className="stage-note" style={{ padding: 14 }}>{cmp.summary.total ? "No changes match this filter." : "No differences found in the compared content."}</p>}
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
                previews?.base.sheets && previews.target.sheets ? (
                  <div className="side-by-side">
                    <div><div className="viewer-heading">v{cmp.base.number} · base</div><SheetWorkspace sheets={previews.base.sheets} styles={previews.base.styles ?? []} readOnly marks={marks.base} focus={focus} label="Base workbook" testId="sheet-base" /></div>
                    <div><div className="viewer-heading">v{cmp.target.number} · target</div><SheetWorkspace sheets={previews.target.sheets} styles={previews.target.styles ?? []} readOnly marks={marks.target} focus={focus} label="Target workbook" testId="sheet-target" /></div>
                  </div>
                ) : <p className="stage-note">Opening both workbooks…</p>
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
