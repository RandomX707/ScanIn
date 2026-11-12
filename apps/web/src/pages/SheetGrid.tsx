import type { Cell, CellStyle, Sheet } from "../lib/api";

const EMU_PER_PX = 9525;
const colLetter = (n: number) => { let s = ""; for (let x = n; x > 0; x = Math.floor((x - 1) / 26)) s = String.fromCharCode(65 + ((x - 1) % 26)) + s; return s; };

/** Spreadsheet-faithful grid: real column widths / row heights, merged ranges, cell styles and the sheet's picture. */
export function SheetGrid({ sheet, styles, edits, onEdit, readOnly }: {
  sheet: Sheet; styles: CellStyle[]; edits?: Record<string, { cell: NonNullable<Cell>; value: string }>;
  onEdit?: (cell: NonNullable<Cell>, value: string) => void; readOnly?: boolean;
}) {
  const layout = sheet.layout;
  const rowsN = Math.max(sheet.rows.length, ...(layout?.merges ?? []).map((m) => m[2]), 0);
  const colsN = Math.min(40, Math.max(...sheet.rows.map((r) => r.length), ...(layout?.merges ?? []).map((m) => m[3]), 0));
  const colPx = (c: number) => Math.round(((layout?.cols[String(c)] ?? layout?.default_col ?? 8.43) || 8.43) * 7 + 5) * ((layout?.cols[String(c)] === 0) ? 0 : 1);
  const rowPx = (r: number) => { const pt = layout?.rows[String(r)] ?? layout?.default_row ?? 15; return Math.round(pt * 4 / 3); };
  const skip = new Set<string>();
  const span = new Map<string, [number, number]>();
  for (const [r1, c1, r2, c2] of layout?.merges ?? []) {
    span.set(`${r1}:${c1}`, [r2 - r1 + 1, Math.min(c2, colsN) - c1 + 1]);
    for (let r = r1; r <= r2; r++) for (let c = c1; c <= c2; c++) if (r !== r1 || c !== c1) skip.add(`${r}:${c}`);
  }
  const css = (r: number, c: number): React.CSSProperties => {
    const st: CellStyle = styles[layout?.cells[`${colLetter(c)}${r}`] ?? -1] ?? {};
    const edge = "1px solid #9aa39b";
    return {
      background: st.bg, color: st.c, fontWeight: st.b ? 700 : undefined, fontStyle: st.i ? "italic" : undefined,
      textDecoration: st.u ? "underline" : undefined, fontSize: st.sz ? `${Math.round(st.sz * 4 / 3)}px` : undefined,
      textAlign: st.ha as React.CSSProperties["textAlign"], verticalAlign: st.va === "center" ? "middle" : st.va ?? "bottom",
      whiteSpace: st.wrap ? "pre-wrap" : "nowrap",
      borderLeft: st.bl ? edge : undefined, borderRight: st.br ? edge : undefined, borderTop: st.bt ? edge : undefined, borderBottom: st.bb ? edge : undefined,
    };
  };
  const width = Array.from({ length: colsN }, (_, i) => colPx(i + 1)).reduce((a, b) => a + b, 0);
  const offX = (col: number, dx: number) => Array.from({ length: col }, (_, i) => colPx(i + 1)).reduce((a, b) => a + b, 0) + dx / EMU_PER_PX;
  const offY = (row: number, dy: number) => Array.from({ length: row }, (_, i) => rowPx(i + 1)).reduce((a, b) => a + b, 0) + dy / EMU_PER_PX;

  return (
    <div className="sheet-fidelity" style={{ position: "relative", width }}>
      <table style={{ tableLayout: "fixed", width }}>
        <colgroup>{Array.from({ length: colsN }, (_, i) => <col key={i} style={{ width: colPx(i + 1) }} />)}</colgroup>
        <tbody>
          {Array.from({ length: rowsN }, (_, r) => (
            <tr key={r} style={{ height: rowPx(r + 1) }}>
              {Array.from({ length: colsN }, (_, c) => {
                if (skip.has(`${r + 1}:${c + 1}`)) return null;
                const [rs, cs] = span.get(`${r + 1}:${c + 1}`) ?? [1, 1];
                const cell: Cell = sheet.rows[r]?.[c] ?? null;
                const style = css(r + 1, c + 1);
                const props = { rowSpan: rs > 1 ? rs : undefined, colSpan: cs > 1 ? cs : undefined, style };
                if (!cell) return <td key={c} {...props} />;
                if (readOnly || !cell.e || !onEdit) return <td key={c} {...props}><div className={readOnly ? "cell-text" : "cell-locked"} title={readOnly ? undefined : cell.r ?? "Locked"}>{cell.v}</div></td>;
                const changed = !!edits && cell.id in edits;
                return (
                  <td key={c} {...props} className={changed ? "cell-changed" : ""}>
                    <input className="cell-input" aria-label={`${sheet.name} ${r + 1}:${c + 1}`} data-cell={cell.id}
                      style={{ font: "inherit", color: "inherit", textAlign: "inherit" }}
                      value={changed ? edits![cell.id].value : cell.v} onChange={(e) => onEdit(cell, e.target.value)} />
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {(layout?.images ?? []).map((img, i) => {
        if (!img.from) return null;
        const left = offX(img.from.col, img.from.dx), top = offY(img.from.row, img.from.dy);
        const w = img.to ? offX(img.to.col, img.to.dx) - left : (img.ext?.cx ?? 0) / EMU_PER_PX;
        const h = img.to ? offY(img.to.row, img.to.dy) - top : (img.ext?.cy ?? 0) / EMU_PER_PX;
        return <img key={i} src={img.src} alt="" style={{ position: "absolute", left, top, width: Math.max(w, 1), height: Math.max(h, 1), pointerEvents: "none", objectFit: "fill" }} />;
      })}
    </div>
  );
}
