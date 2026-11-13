import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, cancelSheetIndex, getSheetMeta, type Cell, type SheetMeta, type SheetBuild } from "../lib/api";
import { HDR_W, VirtualSheet } from "./VirtualSheet";

const ZOOMS = [0.25, 0.4, 0.5, 0.67, 0.75, 0.9, 1, 1.25, 1.5, 2];
export type VirtualWorkbookProps = {
  documentId: string; versionId: string; label?: string; testId?: string; readOnly?: boolean;
  edits?: Record<string, { cell: NonNullable<Cell>; value: string }>; onEdit?: (cell: NonNullable<Cell>, value: string) => void;
  compare?: { base: string; target: string; side: "base" | "target" } | null;
  focus?: { sheet: string; cell: string; nonce: number } | null;
  onReady?: (meta: SheetMeta) => void;
};

/** Excel-like workspace over the windowed index: tabs, go-to cell, formula bar, zoom, fit width, full screen. Never shows a half-built workbook. */
export function VirtualWorkbook({ documentId, versionId, label = "Worksheet", testId, readOnly, edits, onEdit, compare, focus, onReady }: VirtualWorkbookProps) {
  const [meta, setMeta] = useState<SheetMeta | null>(null);
  const [build, setBuild] = useState<SheetBuild | null>({ state: "building", elapsed_s: 0 });
  const [active, setActive] = useState(0);
  const [zoom, setZoom] = useState<number | "fit">(1);
  const [expanded, setExpanded] = useState(false);
  const [selected, setSelected] = useState<{ addr: string; cell: Cell } | null>(null);
  const [goto, setGoto] = useState<{ cell: string; nonce: number } | null>(null);
  const [gotoText, setGotoText] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fitW, setFitW] = useState(0);
  const [box, setBox] = useState(900);
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let live = true; let timer: number | undefined;
    const poll = async () => {
      try {
        const m = await getSheetMeta(documentId, versionId, 4);
        if (!live) return;
        if (m.state === "ready") { setMeta(m as SheetMeta); setBuild(null); onReady?.(m as SheetMeta); return; }
        setBuild(m as SheetBuild);
        if (m.state === "building") timer = window.setTimeout(poll, 800);
      } catch (e) { if (live) setBuild({ state: "failed", error: e instanceof ApiError ? e.code : "network" }); }
    };
    void poll();
    return () => { live = false; window.clearTimeout(timer); };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [documentId, versionId]);

  const visible = (meta?.sheets ?? []).filter((s) => !s.hidden);
  const sheet = visible[Math.min(active, Math.max(0, visible.length - 1))];
  useEffect(() => { if (focus && meta) { const i = visible.findIndex((s) => s.name === focus.sheet); if (i >= 0) setActive(i); setGoto({ cell: focus.cell, nonce: focus.nonce }); } }, [focus?.nonce, meta]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const el = host.current; if (!el) return;
    const measure = () => setBox(el.clientWidth); measure(); const ro = new ResizeObserver(measure); ro.observe(el); return () => ro.disconnect();
  }, [expanded, meta]);
  useEffect(() => {
    if (!expanded) return;
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") setExpanded(false); };
    window.addEventListener("keydown", key); return () => window.removeEventListener("keydown", key);
  }, [expanded]);
  const onMetrics = useCallback((m: { fitWidth: number }) => setFitW(m.fitWidth), []);
  const onLoading = useCallback((v: boolean) => setLoading(v), []);

  if (!meta) {
    const failed = build?.state === "failed" || build?.state === "cancelled";
    return (
      <div className="sheet-workspace virtual-building" data-testid={testId ?? "sheet-viewer"} data-state={build?.state}>
        <div className="viewer-note" role="status">
          {failed ? (build?.state === "cancelled" ? "Preparing this workbook was cancelled." : "This workbook couldn't be prepared for viewing.")
            : `Preparing this large workbook so it can be browsed quickly… ${build?.elapsed_s ? `${Math.round(build.elapsed_s)} s` : ""}`}
          {!failed ? <button className="btn outline small" style={{ marginLeft: 12 }} onClick={() => void cancelSheetIndex(documentId, versionId).catch(() => undefined)}>Cancel</button> : null}
        </div>
      </div>
    );
  }
  if (!sheet) return <p className="stage-note">This workbook has no visible worksheets.</p>;
  const scale = zoom === "fit" ? Math.min(2, Math.max(0.5, (box - 4) / Math.max(fitW, 1))) : zoom;
  const step = (dir: 1 | -1) => { const next = dir > 0 ? ZOOMS.find((z) => z > scale + 0.01) : [...ZOOMS].reverse().find((z) => z < scale - 0.01); if (next) setZoom(next); };
  const jump = () => {
    const raw = gotoText.trim(); const [maybeSheet, addr] = raw.includes("!") ? raw.split("!") : [null, raw];
    if (maybeSheet) { const i = visible.findIndex((s) => s.name.toLowerCase() === maybeSheet.toLowerCase()); if (i >= 0) setActive(i); }
    if (/^[A-Za-z]{1,3}\d{1,7}$/.test(addr)) { setGoto({ cell: addr.toUpperCase(), nonce: Date.now() }); setSelected({ addr: addr.toUpperCase(), cell: null }); }
  };
  const raw = selected?.cell ? selected.cell.v : "";
  return (
    <div className={`sheet-workspace virtual-workbook ${expanded ? "expanded" : ""}`} data-testid={testId ?? "sheet-viewer"} data-virtual="true" ref={host}>
      <div className="viewer-toolbar" role="toolbar" aria-label={`${label} controls`}>
        <div className="vt-group sheet-tabs" role="tablist" aria-label="Worksheets">
          {visible.map((s, i) => <button key={s.name} role="tab" aria-selected={i === active} className={`sheet-tab ${i === active ? "active" : ""}`} onClick={() => { setActive(i); setSelected(null); }}>{s.name}{s.protected ? " 🔒" : ""}</button>)}
        </div>
        <div className="vt-group">
          <form onSubmit={(e) => { e.preventDefault(); jump(); }} className="goto-form"><input aria-label="Go to cell" placeholder="Go to A1" value={gotoText} onChange={(e) => setGotoText(e.target.value)} /><button className="btn outline small" type="submit">Go</button></form>
          <button className="btn outline small" aria-label="Zoom out" onClick={() => step(-1)}>−</button>
          <span className="vt-zoom" aria-label="Zoom level">{Math.round(scale * 100)}%</span>
          <button className="btn outline small" aria-label="Zoom in" onClick={() => step(1)}>+</button>
          <button className={`btn small ${zoom === "fit" ? "dark" : "outline"}`} onClick={() => setZoom("fit")}>Fit width</button>
          <button className={`btn small ${zoom === 1 ? "dark" : "outline"}`} onClick={() => setZoom(1)}>Actual size</button>
          <button className="btn outline small" aria-pressed={expanded} onClick={() => setExpanded((v) => !v)}>{expanded ? "Exit full screen" : "Full screen"}</button>
        </div>
      </div>
      <div className="formula-bar" aria-label="Selected cell">
        <span className="fb-addr">{selected?.addr ?? "—"}</span><span className="sg-fx">fx</span>
        <span className="fb-content">{selected ? (raw || "(empty)") : "Select a cell to see its content"}</span>
        <span className="fb-kind">{loading ? "Loading…" : `${meta.total_cells.toLocaleString()} cells · rows 1–${sheet.max_row.toLocaleString()} · windowed`}</span>
      </div>
      {error ? <div className="viewer-note" role="alert">{error}</div> : null}
      <VirtualSheet key={`${sheet.name}:${versionId}`} documentId={documentId} versionId={versionId} sheet={sheet} styles={meta.styles} zoom={scale} readOnly={readOnly}
        edits={edits} onEdit={onEdit} compare={compare} goto={goto} activeCell={focus && focus.sheet === sheet.name ? focus.cell : null}
        selected={selected?.addr ?? null} onSelect={(addr, cell) => setSelected({ addr, cell })} onMetrics={onMetrics} onLoading={onLoading} onError={setError} />
      <span hidden>{HDR_W}</span>
    </div>
  );
}
