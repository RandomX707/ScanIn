import { useCallback, useEffect, useRef, useState } from "react";
import type { Cell, CellStyle, Sheet } from "../lib/api";
import { SheetGrid, type CellMark } from "../pages/SheetGrid";

const ZOOMS = [0.5, 0.67, 0.75, 0.9, 1, 1.25, 1.5, 2];
export type SheetWorkspaceProps = {
  sheets: Sheet[]; styles: CellStyle[]; edits?: Record<string, { cell: NonNullable<Cell>; value: string }>;
  onEdit?: (cell: NonNullable<Cell>, value: string) => void; readOnly?: boolean;
  /** sheet name -> address -> mark (comparison highlights) */
  marks?: Record<string, Record<string, CellMark>>;
  focus?: { sheet: string; cell: string; nonce: number } | null;
  label?: string; testId?: string;
};

/** Excel-like workspace: sheet tabs, formula bar, zoom, fit width, actual size and full screen. State lives in the parent, so expanding never drops edits. */
export function SheetWorkspace({ sheets, styles, edits, onEdit, readOnly, marks, focus, label = "Worksheet", testId }: SheetWorkspaceProps) {
  const visible = sheets.filter((s) => !s.layout?.hidden);
  const [active, setActive] = useState(0);
  const first = sheets.find((x) => !x.layout?.hidden);
  const [zoom, setZoom] = useState<number | "fit">(() => Math.min(2, Math.max(0.5, (first?.layout?.zoom ?? 100) / 100)));
  const [expanded, setExpanded] = useState(false);
  const [selected, setSelected] = useState<{ addr: string; cell: Cell } | null>(null);
  const [natural, setNatural] = useState(0);
  const [box, setBox] = useState(900);
  const scroller = useRef<HTMLDivElement>(null);
  const sheet = visible[Math.min(active, visible.length - 1)];

  useEffect(() => {
    if (!focus) return;
    const idx = visible.findIndex((s) => s.name === focus.sheet);
    if (idx >= 0) setActive(idx);
  }, [focus, visible.length]);                                  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const el = scroller.current; if (!el) return;
    const measure = () => setBox(el.clientWidth - 4);
    measure(); const ro = new ResizeObserver(measure); ro.observe(el); return () => ro.disconnect();
  }, [expanded, active]);
  useEffect(() => {
    if (!expanded) return;
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") setExpanded(false); };
    window.addEventListener("keydown", key); return () => window.removeEventListener("keydown", key);
  }, [expanded]);
  const onSize = useCallback((_w: number, _h: number, fit: number) => setNatural(fit), []);

  if (!sheet) return <p className="stage-note">This workbook has no visible worksheets.</p>;
  const scale = zoom === "fit" ? Math.min(2, Math.max(0.5, box / Math.max(natural, 1))) : zoom;
  const step = (dir: 1 | -1) => { const cur = scale; const next = dir > 0 ? ZOOMS.find((z) => z > cur + 0.01) : [...ZOOMS].reverse().find((z) => z < cur - 0.01); if (next) setZoom(next); };
  const activeCell = focus && focus.sheet === sheet.name ? focus.cell : null;
  const raw = selected?.cell ? selected.cell.v : "";
  return (
    <div className={`sheet-workspace ${expanded ? "expanded" : ""}`} data-testid={testId ?? "sheet-workspace"}>
      <div className="viewer-toolbar" role="toolbar" aria-label={`${label} controls`}>
        <div className="vt-group sheet-tabs" role="tablist" aria-label="Worksheets">
          {visible.map((s, i) => <button key={s.name} role="tab" aria-selected={i === active} className={`sheet-tab ${i === active ? "active" : ""}`} onClick={() => { setActive(i); setSelected(null); }}>{s.name}{s.layout?.protected ? " 🔒" : ""}</button>)}
        </div>
        <div className="vt-group">
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
        {selected?.cell ? <span className="fb-kind">{String(raw).startsWith("=") && !selected.cell.e ? "Formula · locked · value recalculates in Excel" : selected.cell.e ? "Editable" : selected.cell.r ?? "Read-only"}</span> : null}
      </div>
      <div className="sheet-scroll" ref={scroller} tabIndex={0} aria-label={`${sheet.name} worksheet`}>
        <SheetGrid key={sheet.name} sheet={sheet} styles={styles} edits={edits} onEdit={onEdit} readOnly={readOnly} zoom={scale}
          marks={marks?.[sheet.name]} activeCell={activeCell} selected={selected?.addr ?? null} onSelect={(addr, cell) => setSelected({ addr, cell })} onSize={onSize} />
      </div>
      {sheet.truncated ? <div className="viewer-note">Showing the first 500 rows and 40 columns of this worksheet. The saved file keeps everything.</div> : null}
    </div>
  );
}
