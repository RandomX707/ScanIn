"""Rule-based, cell-anchored sections for spreadsheet templates (RFQ / QCR style) and DOCX quotation forms.

Deterministic; no AI. Every label and value shown is the literal cell/paragraph text. Empty template fields are
reported as "awaiting input" (never invented). Anchors are worksheet cell references or document positions.
"""
from __future__ import annotations

import re
from typing import Any

from openpyxl.utils.cell import get_column_letter

MAX_FACTS = 400


def _short(text: str, limit: int = 160) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _ref(r: int, c: int) -> str:
    return f"{get_column_letter(c)}{r}"


def _style(layout: dict[str, Any] | None, styles: list[dict[str, Any]], r: int, c: int) -> dict[str, Any]:
    idx = ((layout or {}).get("cells") or {}).get(_ref(r, c))
    return styles[idx] if idx is not None and idx < len(styles) else {}


def _chunk(sheet: str, title: str, rows: tuple[int, int], facts: list[dict[str, Any]], texts: list[str], statements: list[dict[str, str]] | None = None) -> dict[str, Any]:
    statements = statements or []
    filled = [f for f in facts if not f["blank"]]
    blank = [f for f in facts if f["blank"]]
    rng = f"rows {rows[0]}–{rows[1]}" if rows[1] != rows[0] else f"row {rows[0]}"
    counts = f"{len(facts)} field{'s' if len(facts) != 1 else ''}" + (f": {len(filled)} filled, {len(blank)} awaiting input" if facts else "")
    points = [f"{f['label']}: {f['value']} ({f['cell']})" for f in filled[:4]]
    if not points:
        points = [f"Awaiting input: {f['label']} ({f['cell']})" for f in blank[:3]]
    if not points:
        points = [f"{_short(st['text'])} ({st['cell']})" for st in statements[:4]]
    elif blank and len(points) < 4:
        points.append(f"Awaiting input: {blank[0]['label']} ({blank[0]['cell']})")
    extra = f"{len(statements)} text line{'s' if len(statements) != 1 else ''}" if statements else ""
    summary = f"{sheet}, {rng}. " + ". ".join(x for x in (counts if facts else "", extra) if x)
    return {"id": "", "title": title, "summary": summary.rstrip(". ") + ".",
            "words": sum(len(t.split()) for t in texts), "points": points[:6],
            "method": "rules: worksheet headings and labels",
            "source": {"kind": "sheet", "label": f"{sheet} · {rng}", "sheet": sheet, "rows": list(rows),
                       "anchors": [{"cell": f["cell"], "label": f["label"]} for f in facts[:60]]},
            "facts": facts[:MAX_FACTS]}


def sheet_chunks(sheet: dict[str, Any], styles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """sheet: {name, rows:[[cell|None]], layout:{merges,cells,...}} as served to the grid."""
    name, rows, layout = sheet["name"], sheet.get("rows") or [], sheet.get("layout")
    span = {}
    for r1, c1, r2, c2 in (layout or {}).get("merges", []):
        span[(r1, c1)] = (r2 - r1 + 1, c2 - c1 + 1)
    grid: dict[tuple[int, int], dict[str, Any]] = {}
    for ri, row in enumerate(rows, start=1):
        for ci, cell in enumerate(row, start=1):
            if cell is not None:
                v = str(cell.get("v") or "")
                grid[(ri, ci)] = {"v": v, "blank": v == "", "formula": v.startswith("=") and not cell.get("e"), "t": cell.get("t")}
    chunks: list[dict[str, Any]] = []
    title, start, facts, texts, colheads, statements = "Header", None, [], [], {}, []

    def flush(end: int) -> None:
        nonlocal facts, texts, statements
        if start is not None and (facts or statements):         # a heading with nothing under it is not a section
            chunks.append(_chunk(name, title, (start, max(end, start)), facts, texts, statements))
        facts, texts, statements = [], [], []

    last_row = 0
    for r in range(1, len(rows) + 1):
        cells = sorted([(c, g) for (rr, c), g in grid.items() if rr == r], key=lambda x: x[0])
        text_cells = [(c, g) for c, g in cells if not g["blank"]]
        if not cells:
            continue
        only = text_cells[0] if len(text_cells) == 1 else None
        if only is not None and not only[1]["formula"]:
            c, g = only
            st = _style(layout, styles, r, c)
            width = span.get((r, c), (1, 1))[1]
            # empty input cells beside the label (outside its own merge) make this row a field, not a heading
            has_inputs = any(cc != c and not (c < cc < c + width) for cc, _ in cells)
            if (not has_inputs and (st.get("b") or st.get("bg") or g["v"].isupper() or width >= 3 or r <= 2)
                    and len(g["v"]) <= 100 and not g["v"].rstrip().endswith(":")):
                flush(last_row)
                title, start, colheads = g["v"], r, {}
                texts.append(g["v"])
                last_row = r
                continue
        if start is None:
            start = r
        last_row = r
        styled_head = len(text_cells) >= 3 and sum(1 for c, _ in text_cells if _style(layout, styles, r, c).get("b") or _style(layout, styles, r, c).get("bg")) >= max(2, len(text_cells) // 2)
        if styled_head:
            colheads = {c: g["v"] for c, g in text_cells}
            texts.extend(g["v"] for _, g in text_cells)
            continue
        i = 0
        while i < len(cells):
            c, g = cells[i]
            if g["blank"]:
                i += 1
                continue
            texts.append(g["v"])
            nxt = cells[i + 1] if i + 1 < len(cells) else None
            if colheads and c in colheads and False:
                pass
            if nxt is not None and not g["formula"]:
                label = g["v"].rstrip(": ").strip()
                nc, ng = nxt
                value = ng["v"]
                facts.append({"label": label, "value": value, "blank": ng["blank"], "cell": _ref(r, nc), "label_cell": _ref(r, c),
                              "formula": ng["formula"]})
                if not ng["blank"]:
                    texts.append(value)
                i += 2
            else:
                if len(cells) == 1 and not g["formula"]:
                    statements.append({"text": g["v"], "cell": _ref(r, c)})
                if colheads and c in colheads and cells and cells[0][0] != c and not cells[0][1]["blank"]:
                    facts.append({"label": f"{cells[0][1]['v'].rstrip(': ').strip()} · {colheads[c]}", "value": g["v"], "blank": False,
                                  "cell": _ref(r, c), "label_cell": _ref(r, cells[0][0]), "formula": g["formula"]})
                i += 1
    flush(last_row)
    for k, ch in enumerate(chunks):
        ch["id"] = f"sheet-{name}-{k + 1}"
    return chunks


def docx_chunks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Quotation-style DOCX: single-cell table rows and heading paragraphs open sections; multi-cell rows are label/value."""
    rows: dict[tuple[int, int], list[dict[str, Any]]] = {}
    order: list[tuple[str, Any]] = []
    for i, b in enumerate(blocks):
        t = b.get("table")
        if t:
            key = (int(t["index"]), int(t["row"]))
            if key not in rows:
                rows[key] = []
                order.append(("row", key))
            rows[key].append(b)
        elif (b.get("text") or "").strip():
            order.append(("para", (i, b)))
    chunks: list[dict[str, Any]] = []
    title, facts, texts, first = "Document", [], [], None

    def flush():
        nonlocal facts, texts, first
        if facts or len(texts) > 1:
            filled = [f for f in facts if f["value"]]
            pts = [f"{f['label']}: {f['value']} ({f['cell']})" for f in filled[:4]] or [t for t in texts[:2]]
            chunks.append({"id": f"docx-{len(chunks) + 1}", "title": title, "summary": (f"{first}. " if first else "") + (f"{len(facts)} labelled value{'s' if len(facts) != 1 else ''}." if facts else ""),
                           "words": sum(len(t.split()) for t in texts), "points": pts[:6], "method": "rules: document headings and table rows",
                           "source": {"kind": "docx", "label": first or "", "anchors": [{"cell": f["cell"], "label": f["label"]} for f in facts[:60]]},
                           "facts": facts[:MAX_FACTS]})
        facts, texts, first = [], [], None

    for kind, payload in order:
        if kind == "para":
            idx, b = payload
            text = b["text"].strip()
            is_head = b.get("heading") is not None or (text.isupper() and len(text) <= 70) or (len(text) <= 60 and text.endswith(":") and False)
            if is_head:
                flush()
                title, first = text, f"Paragraph {idx + 1}"
                texts.append(text)
            else:
                first = first or f"Paragraph {idx + 1}"
                texts.append(text)
            continue
        tbl, row = payload
        cells = sorted(rows[payload], key=lambda b: int(b["table"]["col"]))
        if len(cells) == 1:
            text = cells[0]["text"].strip()
            if text.isupper() and len(text) <= 80:
                flush()
                title, first = text, f"Table {tbl + 1}, row {row + 1}"
            else:
                first = first or f"Table {tbl + 1}, row {row + 1}"      # a lone cell (its value cell is empty): plain text
            texts.append(text)
            continue
        first = first or f"Table {tbl + 1}, row {row + 1}"
        label = cells[0]["text"].strip().rstrip(": ")
        for b in cells[1:]:
            facts.append({"label": label, "value": b["text"].strip(), "blank": not b["text"].strip(), "cell": f"table {tbl + 1}, row {row + 1}, column {int(b['table']['col']) + 1}", "label_cell": f"table {tbl + 1}, row {row + 1}, column 1"})
            texts.append(b["text"])
        texts.append(label)
    flush()
    return chunks
