import { useEffect, useLayoutEffect, useRef } from "react";
import type { Cell, CellStyle, Sheet } from "../lib/api";
import { formatCell } from "../lib/numfmt";

const EMU_PER_PX = 9525;
const ROW_HDR = 44, COL_HDR = 22;
export const colLetter = (n: number) => { let s = ""; for (let x = n; x > 0; x = Math.floor((x - 1) / 26)) s = String.fromCharCode(65 + ((x - 1) % 26)) + s; return s; };
export type CellMark = "add" | "del" | "mod";

type Props = {
  sheet: Sheet; styles: CellStyle[]; edits?: Record<string, { cell: NonNullable<Cell>; value: string }>;
  onEdit?: (cell: NonNullable<Cell>, value: string) => void; readOnly?: boolean;
  /** Excel-style row numbers and column letters (default on). */
  headers?: boolean; zoom?: number;
  /** Changed cells to mark (address like "B7"), used by version comparison. */
  marks?: Record<string, CellMark>; activeCell?: string | null;
  selected?: string | null; onSelect?: (address: string, cell: Cell) => void;
  onSize?: (width: number, height: number, fitWidth: number) => void;
};

/** Spreadsheet-faithful grid: real widths/heights, merged ranges, styles, number formats, frozen panes, hidden rows/columns and the sheet's pictures. */
export function SheetGrid({ sheet, styles, edits, onEdit, readOnly, headers = true, zoom = 1, marks, activeCell, selected, onSelect, onSize }: Props) {
  const layout = sheet.layout;
  const host = useRef<HTMLDivElement>(null);
  const rowsN = Math.max(sheet.rows.length, ...(layout?.merges ?? []).map((m) => m[2]), 0);
  const colsN = Math.min(40, Math.max(...sheet.rows.map((r) => r.length), ...(layout?.merges ?? []).map((m) => m[3]), 0));
  const colPx = (c: number) => { const w = layout?.cols[String(c)]; return w === 0 ? 0 : Math.round(((w ?? layout?.default_col ?? 8.43) || 8.43) * 7 + 5); };
  const rowPx = (r: number) => { const pt = layout?.rows[String(r)]; return pt === 0 ? 0 : Math.round((pt ?? layout?.default_row ?? 15) * 4 / 3); };
  const skip = new Set<string>();
  const span = new Map<string, [number, number]>();
  for (const [r1, c1, r2, c2] of layout?.merges ?? []) {
    span.set(`${r1}:${c1}`, [r2 - r1 + 1, Math.min(c2, colsN) - c1 + 1]);
    for (let r = r1; r <= r2; r++) for (let c = c1; c <= c2; c++) if (r !== r1 || c !== c1) skip.add(`${r}:${c}`);
  }
  const freezeRows = layout?.freeze?.rows ?? 0, freezeCols = layout?.freeze?.cols ?? 0;
  const hdrW = headers ? ROW_HDR : 0, hdrH = headers ? COL_HDR : 0;
  const colLeft = (c: number) => hdrW + Array.from({ length: c - 1 }, (_, i) => colPx(i + 1)).reduce((a, b) => a + b, 0);
  const rowTop = (r: number) => hdrH + Array.from({ length: r - 1 }, (_, i) => rowPx(i + 1)).reduce((a, b) => a + b, 0);
  const css = (r: number, c: number): React.CSSProperties => {
    const st: CellStyle = styles[layout?.cells[`${colLetter(c)}${r}`] ?? -1] ?? {};
    const edge = "1px solid #9aa39b";
    const out: React.CSSProperties = {
      background: st.bg, color: st.c, fontWeight: st.b ? 700 : undefined, fontStyle: st.i ? "italic" : undefined,
      textDecoration: st.u ? "underline" : undefined, fontSize: st.sz ? `${Math.round(st.sz * 4 / 3)}px` : undefined,
      textAlign: st.ha as React.CSSProperties["textAlign"], verticalAlign: st.va === "center" ? "middle" : st.va ?? "bottom",
      whiteSpace: st.wrap ? "pre-wrap" : "nowrap",
      borderLeft: st.bl ? edge : undefined, borderRight: st.br ? edge : undefined, borderTop: st.bt ? edge : undefined, borderBottom: st.bb ? edge : undefined,
    };
    if (r <= freezeRows || c <= freezeCols) {
      out.position = "sticky"; out.zIndex = r <= freezeRows && c <= freezeCols ? 4 : 3;
      if (r <= freezeRows) out.top = rowTop(r);
      if (c <= freezeCols) out.left = colLeft(c);
      out.background = st.bg ?? "#fff";
    }
    return out;
  };
  const width = hdrW + Array.from({ length: colsN }, (_, i) => colPx(i + 1)).reduce((a, b) => a + b, 0);
  const height = hdrH + Array.from({ length: rowsN }, (_, i) => rowPx(i + 1)).reduce((a, b) => a + b, 0);
  const offX = (col: number, dx: number) => colLeft(col + 1) + dx / EMU_PER_PX;
  const offY = (row: number, dy: number) => rowTop(row + 1) + dy / EMU_PER_PX;

  // Width of the used range (cells with content, locked cells, merged ranges): what "fit width" should fit.
  let contentCols = 0;
  sheet.rows.forEach((row) => row.forEach((cell, c) => { if (cell && (cell.v !== "" || !cell.e)) contentCols = Math.max(contentCols, c + 1); }));
  for (const m of layout?.merges ?? []) contentCols = Math.max(contentCols, Math.min(m[3], colsN));
  const fitWidth = hdrW + Array.from({ length: Math.max(contentCols, 1) }, (_, i) => colPx(i + 1)).reduce((a, b) => a + b, 0);
  useLayoutEffect(() => { onSize?.(width, height, fitWidth); }, [width, height, fitWidth, onSize]);
  useEffect(() => {
    if (!activeCell) return;
    host.current?.querySelector(`[data-addr="${activeCell}"]`)?.scrollIntoView({ block: "center", inline: "center" });
  }, [activeCell]);

  return (
    <div ref={host} className="sheet-fidelity" style={{ position: "relative", width, zoom }} data-sheet={sheet.name}>
      <table style={{ tableLayout: "fixed", width }}>
        <colgroup>{headers ? <col style={{ width: ROW_HDR }} /> : null}{Array.from({ length: colsN }, (_, i) => <col key={i} style={{ width: colPx(i + 1) }} />)}</colgroup>
        {headers ? (
          <thead><tr style={{ height: COL_HDR }}>
            <th className="sg-corner" style={{ position: "sticky", top: 0, left: 0, zIndex: 6 }} />
            {Array.from({ length: colsN }, (_, i) => colPx(i + 1) === 0 ? null : <th key={i} className="sg-colhdr" style={{ position: "sticky", top: 0, zIndex: i < freezeCols ? 5 : 2 }}>{colLetter(i + 1)}</th>)}
          </tr></thead>
        ) : null}
        <tbody>
          {Array.from({ length: rowsN }, (_, r) => {
            const h = rowPx(r + 1);
            if (h === 0) return null;                                  // hidden row
            return (
              <tr key={r} style={{ height: h }}>
                {headers ? <th className="sg-rowhdr" style={{ position: "sticky", left: 0, zIndex: r < freezeRows ? 5 : 2, ...(r < freezeRows ? { top: rowTop(r + 1) } : {}) }}>{r + 1}</th> : null}
                {Array.from({ length: colsN }, (_, c) => {
                  if (colPx(c + 1) === 0 || skip.has(`${r + 1}:${c + 1}`)) return null;
                  const [rs, cs] = span.get(`${r + 1}:${c + 1}`) ?? [1, 1];
                  const cell: Cell = sheet.rows[r]?.[c] ?? null;
                  const addr = `${colLetter(c + 1)}${r + 1}`;
                  const mark = marks?.[addr];
                  const classes = ["sg-cell", mark ? `sg-mark-${mark}` : "", activeCell === addr ? "sg-active" : "", selected === addr ? "sg-selected" : ""].filter(Boolean).join(" ");
                  const props = { rowSpan: rs > 1 ? rs : undefined, colSpan: cs > 1 ? cs : undefined, style: css(r + 1, c + 1), "data-addr": addr, className: classes, onClick: () => onSelect?.(addr, cell) };
                  if (!cell) return <td key={c} {...props} />;
                  const styleOf: CellStyle = styles[layout?.cells[addr] ?? -1] ?? {};
                  const isFormula = !cell.e && String(cell.v).startsWith("=");
                  const isProtected = !cell.e && (cell.r ?? "").toLowerCase().includes("protect");
                  if (readOnly || !cell.e || !onEdit) {
                    const shown = isFormula ? cell.v : formatCell(cell.v, cell.t, styleOf.nf);
                    return <td key={c} {...props} className={`${classes} ${isFormula ? "sg-formula" : ""} ${isProtected ? "sg-protected" : ""}`}>
                      <div className={readOnly ? "cell-text" : "cell-locked"} title={readOnly ? (isFormula ? "Formula: its value recalculates in Excel" : undefined) : cell.r ?? "Locked"}>{isFormula ? <><span className="sg-fx">fx</span>{shown}</> : shown}</div>
                    </td>;
                  }
                  const changed = !!edits && cell.id in edits;
                  return (
                    <td key={c} {...props} className={`${classes} ${changed ? "cell-changed" : ""} sg-editable`}>
                      <input className="cell-input" aria-label={`${sheet.name} ${r + 1}:${c + 1}`} data-cell={cell.id}
                        style={{ font: "inherit", color: "inherit", textAlign: "inherit" }}
                        value={changed ? edits![cell.id].value : cell.v} onFocus={() => onSelect?.(addr, cell)} onChange={(e) => onEdit(cell, e.target.value)} />
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
      {(layout?.images ?? []).map((img, i) => {
        if (!img.from) return null;
        const left = offX(img.from.col, img.from.dx), top = offY(img.from.row, img.from.dy);
        const w = img.to ? offX(img.to.col, img.to.dx) - left : (img.ext?.cx ?? 0) / EMU_PER_PX;
        const h = img.to ? offY(img.to.row, img.to.dy) - top : (img.ext?.cy ?? 0) / EMU_PER_PX;
        return <img key={i} src={img.src} alt="" style={{ position: "absolute", left, top, width: Math.max(w, 1), height: Math.max(h, 1), pointerEvents: "none", objectFit: "fill", zIndex: 1 }} />;
      })}
    </div>
  );
}
