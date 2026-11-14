import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { getCompareMarks, getSheetCell, getSheetWindow, type Cell, type CellStyle, type SheetMetaSheet, type WindowCell } from "../lib/api";
import { formatCell } from "../lib/numfmt";
import { colLetter } from "../pages/SheetGrid";

export const HDR_W = 44, HDR_H = 22;
const EMU_PER_PX = 9525, BR = 100, BC = 40, KEY = 100_000, MAX_BLOCKS = 90;
type Kind = "added" | "removed" | "modified";
const MARK_CLASS: Record<Kind, string> = { added: "sg-mark-add", removed: "sg-mark-del", modified: "sg-mark-mod" };

export type VirtualSheetProps = {
  documentId: string; versionId: string; sheet: SheetMetaSheet; styles: CellStyle[]; zoom: number; readOnly?: boolean;
  edits?: Record<string, { cell: NonNullable<Cell>; value: string }>; onEdit?: (cell: NonNullable<Cell>, value: string) => void;
  compare?: { base: string; target: string; side: "base" | "target" } | null;
  activeCell?: string | null; goto?: { cell: string; nonce: number } | null;
  selected?: string | null; onSelect?: (address: string, cell: Cell) => void;
  onMetrics?: (m: { fitWidth: number }) => void; onLoading?: (loading: boolean) => void; onError?: (message: string) => void;
};

const parseAddr = (a: string): [number, number] | null => {
  const m = /^([A-Za-z]{1,3})(\d{1,7})$/.exec(a.trim()); if (!m) return null;
  let c = 0; for (const ch of m[1].toUpperCase()) c = c * 26 + ch.charCodeAt(0) - 64;
  return [Number(m[2]), c];
};
export const addrOf = (r: number, c: number) => `${colLetter(c)}${r}`;
const lowerBound = (arr: ArrayLike<number>, x: number, hi: number) => { let lo = 0, h = hi; while (lo < h) { const mid = (lo + h) >> 1; if (arr[mid] <= x) lo = mid + 1; else h = mid; } return lo; };

/** Viewport-virtualised worksheet: only the visible cells exist in the DOM; cell data arrives in blocks from the windowed index. */
export function VirtualSheet(props: VirtualSheetProps) {
  const { documentId, versionId, sheet, styles, zoom, readOnly, edits, onEdit, compare, activeCell, goto, selected, onSelect, onMetrics, onLoading, onError } = props;
  const scroller = useRef<HTMLDivElement>(null);
  const [scroll, setScroll] = useState({ x: 0, y: 0 });
  const [view, setView] = useState({ w: 800, h: 500 });
  const [tick, setTick] = useState(0);
  const [editing, setEditing] = useState<{ r: number; c: number; value: string } | null>(null);
  const cells = useRef(new Map<number, WindowCell>());
  const marks = useRef(new Map<number, Kind>());
  const blocks = useRef(new Map<string, number>());          // block key -> last-used counter
  const pending = useRef(new Set<string>());
  const counter = useRef(0);
  const raf = useRef(0);

  const geo = useMemo(() => {
    const maxCol = Math.max(sheet.max_col, 1), maxRow = Math.max(sheet.max_row, 1);
    const defW = Math.round(((sheet.default_col ?? 8.43) || 8.43) * 7 + 5), defH = Math.round((sheet.default_row ?? 15) * 4 / 3);
    const left = new Float64Array(maxCol + 2); let acc = 0;
    for (let c = 1; c <= maxCol; c++) { left[c] = acc; const w = sheet.cols[String(c)]; acc += w === 0 ? 0 : w !== undefined ? Math.round(w * 7 + 5) : defW; }
    left[maxCol + 1] = acc;
    const custom = Object.entries(sheet.rows).map(([r, pt]) => [Number(r), pt === 0 ? 0 : Math.round(pt * 4 / 3)] as [number, number]).filter(([r]) => r <= maxRow).sort((a, b) => a[0] - b[0]);
    const cRows = custom.map(([r]) => r); const cum = new Float64Array(custom.length + 1);
    custom.forEach(([, h], i) => { cum[i + 1] = cum[i] + (h - defH); });
    const y = (r: number) => (r - 1) * defH + cum[lowerBound(cRows, r - 1, cRows.length)];       // top edge of row r
    const rowH = (r: number) => { const pt = sheet.rows[String(r)]; return pt === undefined ? defH : pt === 0 ? 0 : Math.round(pt * 4 / 3); };
    const rowAt = (py: number) => { let lo = 1, hi = maxRow; while (lo < hi) { const mid = (lo + hi) >> 1; if (y(mid + 1) <= py) lo = mid + 1; else hi = mid; } return lo; };
    const colAt = (px: number) => Math.max(1, Math.min(maxCol, lowerBound(left.subarray(1, maxCol + 1), px, maxCol)));
    return { maxCol, maxRow, left, y, rowH, rowAt, colAt, totalW: left[maxCol + 1], totalH: y(maxRow + 1), defW };
  }, [sheet]);

  const fr = sheet.freeze?.rows ?? 0, fc = sheet.freeze?.cols ?? 0;
  const sx = scroll.x, sy = scroll.y;
  // visible content range (in cells)
  const range = useMemo(() => {
    const px0 = sx / zoom, px1 = (sx + view.w - HDR_W) / zoom, py0 = sy / zoom, py1 = (sy + view.h - HDR_H) / zoom;
    const c0 = geo.colAt(px0), c1 = geo.colAt(Math.min(px1, geo.totalW - 1)), r0 = geo.rowAt(py0), r1 = geo.rowAt(Math.min(py1, geo.totalH - 1));
    return { r0, r1: Math.min(r1, geo.maxRow), c0, c1: Math.min(c1, geo.maxCol) };
  }, [sx, sy, zoom, view, geo]);

  // ---- data loading (blocks) -------------------------------------------------------------------------------------------------
  const need = useMemo(() => {
    const want = new Set<string>();
    const add = (ra: number, rb: number, ca: number, cb: number) => {
      for (let br = Math.floor((Math.max(ra, 1) - 1) / BR); br <= Math.floor((Math.min(rb, geo.maxRow) - 1) / BR); br++)
        for (let bc = Math.floor((Math.max(ca, 1) - 1) / BC); bc <= Math.floor((Math.min(cb, geo.maxCol) - 1) / BC); bc++) want.add(`${br}:${bc}`);
    };
    const { r0, r1, c0, c1 } = range;
    add(r0 - 15, r1 + 15, c0 - 3, c1 + 3);
    if (fr) add(1, fr, c0, c1); if (fc) add(r0, r1, 1, fc); if (fr && fc) add(1, fr, 1, fc);
    for (const [mr1, mc1, mr2, mc2] of sheet.merges) if (mr2 >= r0 && mr1 <= r1 && mc2 >= c0 && mc1 <= c1) add(mr1, mr1, mc1, mc1);       // merged origin may be off-screen
    return [...want];
  }, [range, geo, sheet.merges, fr, fc]);

  useEffect(() => {
    let live = true;
    for (const key of need) {
      blocks.current.set(key, ++counter.current);
      if (pending.current.has(key) || (blocks.current.get(`done:${key}`) ?? 0)) continue;
      const [br, bc] = key.split(":").map(Number);
      const r1 = br * BR + 1, r2 = Math.min((br + 1) * BR, geo.maxRow), c1 = bc * BC + 1, c2 = Math.min((bc + 1) * BC, geo.maxCol);
      pending.current.add(key); onLoading?.(true);
      const jobs: Promise<unknown>[] = [getSheetWindow(documentId, versionId, sheet.name, r1, r2, c1, c2).then((w) => {
        for (const cell of w.cells ?? []) cells.current.set(cell[0] * KEY + cell[1], cell);
      })];
      if (compare) jobs.push(getCompareMarks(documentId, compare.base, compare.target, sheet.name, r1, r2, c1, c2).then((m) => {
        for (const [r, c, kind] of m.marks) {
          // base side shows removed/modified, target side shows added/modified
          if (compare.side === "base" ? kind !== "added" : kind !== "removed") marks.current.set(r * KEY + c, kind);
        }
      }));
      Promise.all(jobs).then(() => { blocks.current.set(`done:${key}`, 1); }).catch(() => { if (live) onError?.("Part of the worksheet could not be loaded."); })
        .finally(() => { pending.current.delete(key); if (live) { setTick((t) => t + 1); if (pending.current.size === 0) onLoading?.(false); } });
    }
    // bound memory: forget the least recently used blocks' cells when too many are resident
    const resident = [...blocks.current.entries()].filter(([k]) => !k.startsWith("done:"));
    if (resident.length > MAX_BLOCKS) {
      const drop = resident.sort((a, b) => a[1] - b[1]).slice(0, resident.length - MAX_BLOCKS).map(([k]) => k);
      for (const k of drop) {
        const [br, bc] = k.split(":").map(Number); blocks.current.delete(k); blocks.current.delete(`done:${k}`);
        for (let r = br * BR + 1; r <= (br + 1) * BR; r++) for (let c = bc * BC + 1; c <= (bc + 1) * BC; c++) { cells.current.delete(r * KEY + c); marks.current.delete(r * KEY + c); }
      }
    }
    return () => { live = false; };
  }, [need, documentId, versionId, sheet.name, geo.maxRow, geo.maxCol, compare, onLoading, onError]);

  // ---- scrolling & sizing -----------------------------------------------------------------------------------------------------
  useLayoutEffect(() => {
    const el = scroller.current; if (!el) return;
    const measure = () => setView({ w: el.clientWidth, h: el.clientHeight });
    measure(); const ro = new ResizeObserver(measure); ro.observe(el); return () => ro.disconnect();
  }, []);
  const onScroll = useCallback(() => {
    if (raf.current) return;
    raf.current = requestAnimationFrame(() => { raf.current = 0; const el = scroller.current; if (el) setScroll({ x: el.scrollLeft, y: el.scrollTop }); });
  }, []);
  useEffect(() => { onMetrics?.({ fitWidth: HDR_W + geo.left[Math.min((sheet.content_col ?? geo.maxCol) + 1, geo.maxCol + 1)] }); }, [geo, sheet.content_col, onMetrics]);
  const scrollToCell = useCallback((r: number, c: number, center = true) => {
    const el = scroller.current; if (!el) return;
    const x = geo.left[Math.min(c, geo.maxCol)] * zoom, yy = geo.y(Math.min(r, geo.maxRow)) * zoom;
    el.scrollTo({ left: Math.max(0, center ? x - (view.w - HDR_W) / 2 : x), top: Math.max(0, center ? yy - (view.h - HDR_H) / 2 : yy) });
  }, [geo, zoom, view]);
  useEffect(() => { const a = goto && parseAddr(goto.cell); if (a) scrollToCell(a[0], a[1]); }, [goto?.nonce]);    // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { const a = activeCell ? parseAddr(activeCell) : null; if (a) scrollToCell(a[0], a[1]); }, [activeCell]);   // eslint-disable-line react-hooks/exhaustive-deps

  // ---- interaction -------------------------------------------------------------------------------------------------------------
  const cellObj = (r: number, c: number): Cell => {
    const w = cells.current.get(r * KEY + c);
    return w ? { v: w[2], t: w[3] ?? "", id: `xlsx:${sheet.name}!${addrOf(r, c)}`, fp: "", e: w[5] === 1, r: w[6] } as unknown as Cell : null;
  };
  const select = (r: number, c: number) => onSelect?.(addrOf(r, c), cellObj(r, c));
  const selRC = selected ? parseAddr(selected) : null;
  const commit = async (r: number, c: number, value: string) => {
    setEditing(null);
    const w = cells.current.get(r * KEY + c); const id = `xlsx:${sheet.name}!${addrOf(r, c)}`;
    if (!w || !onEdit) return;
    const existing = edits?.[id];
    if (value === (existing ? existing.value : w[2])) return;
    try {
      const d = await getSheetCell(documentId, versionId, sheet.name, r, c);
      onEdit({ v: d.v, t: d.t ?? "", id: d.id, fp: d.fp, e: d.e, r: d.r } as unknown as NonNullable<Cell>, value);
    } catch { onError?.("That cell could not be prepared for editing."); }
  };
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (editing) return;
    const cur = selRC ?? [1, 1];
    const move = (dr: number, dc: number) => { e.preventDefault(); const r = Math.min(Math.max(1, cur[0] + dr), geo.maxRow), c = Math.min(Math.max(1, cur[1] + dc), geo.maxCol); select(r, c);
      const el = scroller.current; if (el) { const top = geo.y(r) * zoom, bottom = geo.y(r + 1) * zoom, left = geo.left[c] * zoom, right = geo.left[c + 1] * zoom;
        if (top < el.scrollTop) el.scrollTop = top; else if (bottom > el.scrollTop + el.clientHeight - HDR_H) el.scrollTop = bottom - el.clientHeight + HDR_H;
        if (left < el.scrollLeft) el.scrollLeft = left; else if (right > el.scrollLeft + el.clientWidth - HDR_W) el.scrollLeft = right - el.clientWidth + HDR_W; } };
    if (e.key === "ArrowDown") move(1, 0); else if (e.key === "ArrowUp") move(-1, 0); else if (e.key === "ArrowRight") move(0, 1); else if (e.key === "ArrowLeft") move(0, -1);
    else if (e.key === "PageDown") move(Math.max(1, range.r1 - range.r0 - 2), 0); else if (e.key === "PageUp") move(-Math.max(1, range.r1 - range.r0 - 2), 0);
    else if ((e.key === "Enter" || e.key === "F2") && !readOnly && onEdit && selRC) { const w = cells.current.get(selRC[0] * KEY + selRC[1]); if (w && w[5] === 1) { e.preventDefault(); setEditing({ r: selRC[0], c: selRC[1], value: edits?.[`xlsx:${sheet.name}!${selected}`]?.value ?? w[2] }); } }
  };

  // ---- rendering ---------------------------------------------------------------------------------------------------------------
  const out: React.ReactNode[] = [];
  const covered = new Set<number>(); const mergeAt = new Map<number, [number, number]>();
  for (const [mr1, mc1, mr2, mc2] of sheet.merges) {
    if (mr2 < range.r0 || mr1 > range.r1 || mc2 < range.c0 || mc1 > range.c1) continue;
    mergeAt.set(mr1 * KEY + mc1, [mr2, mc2]);
    for (let r = Math.max(mr1, range.r0); r <= Math.min(mr2, range.r1); r++) for (let c = Math.max(mc1, range.c0); c <= Math.min(mc2, range.c1); c++) if (r !== mr1 || c !== mc1) covered.add(r * KEY + c);
  }
  const frozenRowSet = (r: number) => r <= fr, frozenColSet = (c: number) => c <= fc;
  const place = (r: number, c: number, r2: number, c2: number) => {
    const left = HDR_W + geo.left[c] * zoom - (frozenColSet(c) ? 0 : sx), top = HDR_H + geo.y(r) * zoom - (frozenRowSet(r) ? 0 : sy);
    return { left, top, width: (geo.left[c2 + 1] - geo.left[c]) * zoom, height: (geo.y(r2 + 1) - geo.y(r)) * zoom };
  };
  const drawCell = (r: number, c: number, r2: number, c2: number, z: number) => {
    const w = cells.current.get(r * KEY + c); const st: CellStyle = (w && w[4] >= 0 ? styles[w[4]] : undefined) ?? {};
    const addr = addrOf(r, c); const id = `xlsx:${sheet.name}!${addr}`; const edited = edits?.[id];
    const isFormula = !!w && w[5] === 0 && w[2].startsWith("=");
    const mark = marks.current.get(r * KEY + c);
    const pos = place(r, c, r2, c2);
    if (pos.width <= 0 || pos.height <= 0) return null;
    const edge = "1px solid #9aa39b";
    const style: React.CSSProperties = { position: "absolute", ...pos, background: st.bg ?? (z > 0 ? "#fff" : undefined), color: st.c, fontWeight: st.b ? 700 : undefined, fontStyle: st.i ? "italic" : undefined,
      textDecoration: st.u ? "underline" : undefined, fontSize: `${Math.max(6, Math.round((st.sz ? st.sz * 4 / 3 : 16) * zoom))}px`, textAlign: st.ha as React.CSSProperties["textAlign"],
      display: "flex", alignItems: st.va === "top" ? "flex-start" : st.va === "center" ? "center" : "flex-end", justifyContent: st.ha === "center" ? "center" : st.ha === "right" ? "flex-end" : "flex-start",
      whiteSpace: st.wrap ? "pre-wrap" : "nowrap", overflow: "hidden", padding: `0 ${3 * zoom}px`, boxSizing: "border-box", zIndex: z,
      borderLeft: st.bl ? edge : undefined, borderRight: st.br ? edge : undefined, borderTop: st.bt ? edge : undefined, borderBottom: st.bb ? edge : undefined };
    const classes = ["sg-cell", "sg-v", mark ? MARK_CLASS[mark] : "", activeCell === addr ? "sg-active" : "", selected === addr ? "sg-selected" : "", isFormula ? "sg-formula" : "", w && w[5] === 1 && !readOnly ? "sg-editable" : "", edited ? "cell-changed" : ""].filter(Boolean).join(" ");
    const shown = edited ? edited.value : isFormula ? w![2] : w ? formatCell(w[2], w[3], st.nf) : "";
    return (
      <div key={`${r}:${c}`} className={classes} data-addr={addr} style={style} onClick={() => select(r, c)}
        onDoubleClick={() => { if (!readOnly && onEdit && w && w[5] === 1) setEditing({ r, c, value: edited ? edited.value : w[2] }); }}>
        {isFormula ? <span className="sg-fx">fx</span> : null}<span className="cell-text">{shown}</span>
      </div>
    );
  };
  // scrolling body first, then frozen areas on top
  for (let r = range.r0; r <= range.r1; r++) { if (geo.rowH(r) === 0 || frozenRowSet(r)) continue;
    for (let c = range.c0; c <= range.c1; c++) { if (geo.left[c + 1] - geo.left[c] === 0 || frozenColSet(c)) continue;
      const k = r * KEY + c; if (covered.has(k)) continue; const m = mergeAt.get(k); out.push(drawCell(r, c, m ? m[0] : r, m ? m[1] : c, 1)); } }
  for (const [k, [mr2, mc2]] of mergeAt) { const mr = Math.floor(k / KEY), mc = k % KEY; if ((mr < range.r0 || mc < range.c0) && !frozenRowSet(mr) && !frozenColSet(mc)) out.push(drawCell(mr, mc, mr2, mc2, 1)); }
  for (let r = 1; r <= fr; r++) { if (geo.rowH(r) === 0) continue; for (let c = range.c0; c <= range.c1; c++) { if (frozenColSet(c)) continue; if (!covered.has(r * KEY + c)) out.push(drawCell(r, c, r, c, 2)); } }
  for (let c = 1; c <= fc; c++) { if (geo.left[c + 1] - geo.left[c] === 0) continue; for (let r = range.r0; r <= range.r1; r++) { if (frozenRowSet(r) || geo.rowH(r) === 0) continue; if (!covered.has(r * KEY + c)) out.push(drawCell(r, c, r, c, 2)); } }
  for (let r = 1; r <= fr; r++) for (let c = 1; c <= fc; c++) out.push(drawCell(r, c, r, c, 3));
  // pictures at their anchors
  const imgs = sheet.images.map((img, i) => {
    if (!img.from) return null;
    const l = geo.left[Math.min(img.from.col + 1, geo.maxCol + 1)] + img.from.dx / EMU_PER_PX, t = geo.y(Math.min(img.from.row + 1, geo.maxRow + 1)) + img.from.dy / EMU_PER_PX;
    const w = img.to ? geo.left[Math.min(img.to.col + 1, geo.maxCol + 1)] + img.to.dx / EMU_PER_PX - l : (img.ext?.cx ?? 0) / EMU_PER_PX;
    const h = img.to ? geo.y(Math.min(img.to.row + 1, geo.maxRow + 1)) + img.to.dy / EMU_PER_PX - t : (img.ext?.cy ?? 0) / EMU_PER_PX;
    const left = HDR_W + l * zoom - sx, top = HDR_H + t * zoom - sy;
    if (left > view.w || top > view.h || left + w * zoom < HDR_W || top + h * zoom < HDR_H) return null;
    return <img key={`img${i}`} src={img.src} alt="" style={{ position: "absolute", left, top, width: Math.max(w * zoom, 1), height: Math.max(h * zoom, 1), pointerEvents: "none", zIndex: 1 }} />;
  });
  // headers
  const hdr: React.ReactNode[] = [];
  for (let c = range.c0; c <= range.c1; c++) { const w = (geo.left[c + 1] - geo.left[c]) * zoom; if (w <= 0) continue; hdr.push(<div key={`ch${c}`} className="sg-colhdr sg-vh" style={{ position: "absolute", left: HDR_W + geo.left[c] * zoom - sx, top: 0, width: w, height: HDR_H, zIndex: 5 }}>{colLetter(c)}</div>); }
  for (let c = 1; c <= fc; c++) { const w = (geo.left[c + 1] - geo.left[c]) * zoom; if (w > 0) hdr.push(<div key={`fch${c}`} className="sg-colhdr sg-vh" style={{ position: "absolute", left: HDR_W + geo.left[c] * zoom, top: 0, width: w, height: HDR_H, zIndex: 6 }}>{colLetter(c)}</div>); }
  for (let r = range.r0; r <= range.r1; r++) { const h = geo.rowH(r) * zoom; if (h <= 0 || frozenRowSet(r)) continue; hdr.push(<div key={`rh${r}`} className="sg-rowhdr sg-vh" style={{ position: "absolute", left: 0, top: HDR_H + geo.y(r) * zoom - sy, width: HDR_W, height: h, zIndex: 5 }}>{r}</div>); }
  for (let r = 1; r <= fr; r++) { const h = geo.rowH(r) * zoom; if (h > 0) hdr.push(<div key={`frh${r}`} className="sg-rowhdr sg-vh" style={{ position: "absolute", left: 0, top: HDR_H + geo.y(r) * zoom, width: HDR_W, height: h, zIndex: 6 }}>{r}</div>); }
  void tick;
  const edit = editing ? (() => { const p = place(editing.r, editing.c, editing.r, editing.c); return (
    <input autoFocus className="cell-input vs-editor" aria-label={`Edit ${addrOf(editing.r, editing.c)}`} data-cell={`xlsx:${sheet.name}!${addrOf(editing.r, editing.c)}`}
      style={{ position: "absolute", ...p, zIndex: 9, boxSizing: "border-box", font: "inherit", border: "2px solid #2c6a4c", background: "#fff", color: "#182925" }}
      value={editing.value} onChange={(e) => setEditing({ ...editing, value: e.target.value })} onBlur={() => void commit(editing.r, editing.c, editing.value)}
      onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void commit(editing.r, editing.c, editing.value); } else if (e.key === "Escape") setEditing(null); e.stopPropagation(); }} />); })() : null;

  return (
    <div className="vs-scroll sheet-scroll" ref={scroller} onScroll={onScroll} tabIndex={0} onKeyDown={onKeyDown} role="grid" aria-label={`${sheet.name} worksheet (virtualised)`} data-sheet={sheet.name}
      data-rendered={range.r1 - range.r0 + 1} style={{ position: "relative" }}>
      <div className="vs-layer" style={{ position: "sticky", top: 0, left: 0, width: view.w, height: view.h, marginBottom: -view.h, overflow: "hidden", background: "#fff" }}>
        <div className="sg-corner sg-vh" style={{ position: "absolute", left: 0, top: 0, width: HDR_W, height: HDR_H, zIndex: 7 }} />
        {out}{imgs}{hdr}{edit}
      </div>
      <div className="vs-spacer" style={{ width: HDR_W + geo.totalW * zoom, height: HDR_H + geo.totalH * zoom }} />
    </div>
  );
}
