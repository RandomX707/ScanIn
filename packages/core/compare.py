"""Pure version-comparison engine: normalised snapshots of two persisted versions in, change records out.

Snapshots are produced from the persisted version FILES by the sandboxed workers (never from replayed edit events).
This module contains no I/O: it only aligns and diffs those snapshots, so it is fully unit-testable.

Change record: {id, kind: added|removed|modified, category: text|value|formula|format|structure|image,
                location: {...}, label, before, after, base_bbox?, target_bbox?, page?}
"""
from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from typing import Any

MAX_CHANGES = 200_000
MAX_CHANGE_BYTES = 16 * 1024 * 1024


class _BoundedChanges:
    """Keep a deterministic prefix while counting every discovered change."""
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.counts = {"added": 0, "removed": 0, "modified": 0}
        self.total = 0
        self.bytes = 0

    def append(self, change: dict[str, Any]) -> None:
        self.total += 1
        self.counts[change["kind"]] += 1
        size = len(json.dumps(change, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
        if len(self.items) < MAX_CHANGES and self.bytes + size + 32 <= MAX_CHANGE_BYTES:
            self.items.append(change)
            self.bytes += size


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b, autojunk=False).ratio() if (a or b) else 1.0


def _align(base: list[str], target: list[str]) -> list[tuple[str, int | None, int | None]]:
    """Align two ordered text lists. Yields ('same'|'mod'|'del'|'add', base_index, target_index)."""
    keys_b, keys_t = [_norm(x) for x in base], [_norm(x) for x in target]
    out: list[tuple[str, int | None, int | None]] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, keys_b, keys_t, autojunk=False).get_opcodes():
        if tag == "equal":
            out.extend(("same", i1 + k, j1 + k) for k in range(i2 - i1))
        elif tag == "delete":
            out.extend(("del", i, None) for i in range(i1, i2))
        elif tag == "insert":
            out.extend(("add", None, j) for j in range(j1, j2))
        else:                                                 # replace: pair the most similar neighbours in order
            bi, ti = list(range(i1, i2)), list(range(j1, j2))
            if len(bi) == len(ti):                            # a one-for-one replacement is a modification, however large
                out.extend(("mod", i, j) for i, j in zip(bi, ti))
                continue
            pos = 0
            for i in bi:
                best = None
                for k in range(pos, len(ti)):
                    s = _sim(keys_b[i], keys_t[ti[k]])
                    if s >= 0.45 and (best is None or s > best[0]):
                        best = (s, k)
                if best is None:
                    out.append(("del", i, None))
                else:
                    for k in range(pos, best[1]):
                        out.append(("add", None, ti[k]))
                    out.append(("mod", i, ti[best[1]]))
                    pos = best[1] + 1
            out.extend(("add", None, ti[k]) for k in range(pos, len(ti)))
    return out


def _summary(changes: list[dict[str, Any]], truncated: bool, total: int | None = None) -> dict[str, Any]:
    counts = {"added": 0, "removed": 0, "modified": 0}
    for c in changes:
        counts[c["kind"]] += 1
    return {**counts, "total": len(changes) if total is None else total, "truncated": truncated}


def _finish(changes: list[dict[str, Any]] | _BoundedChanges, notes: list[str]) -> dict[str, Any]:
    """Finalize either a bounded full scan or an in-memory legacy diff result."""
    bounded = isinstance(changes, _BoundedChanges)
    total = changes.total if bounded else len(changes)
    retained = changes.items if bounded else changes[:MAX_CHANGES]
    cut = len(retained) < total
    summary = ({**changes.counts, "total": total, "truncated": cut} if bounded else _summary(changes, cut, total))
    changes = retained
    for index, change in enumerate(changes):
        change["id"] = f"c{index + 1}"
    retained_bytes = changes_bytes(changes)
    return {"changes": changes, "summary": summary, "limits": notes, "complete": (True if bounded else not cut),
            "scan_status": "COMPLETE" if bounded else ("PARTIAL" if cut else "COMPLETE"),
            "list_status": "TRUNCATED" if cut else "COMPLETE", "total_changes_detected": total,
            "returned_changes": len(changes), "has_more": cut,
            "retained_change_bytes": retained_bytes}


def changes_bytes(changes: list[dict[str, Any]]) -> int:
    return sum(len(json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")) for item in changes)


# ------------------------------------------------------------------ PDF
def diff_pdf(base: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    pages_b = {p["page"]: p for p in base.get("pages", [])}
    pages_t = {p["page"]: p for p in target.get("pages", [])}
    changes: list[dict[str, Any]] = []
    for number in sorted(set(pages_b) | set(pages_t)):
        pb, pt = pages_b.get(number), pages_t.get(number)
        lines_b = [ln for ln in (pb or {}).get("lines", [])]
        lines_t = [ln for ln in (pt or {}).get("lines", [])]
        for tag, i, j in _align([l["t"] for l in lines_b], [l["t"] for l in lines_t]):
            if tag == "same":
                continue
            lb = lines_b[i] if i is not None else None
            lt = lines_t[j] if j is not None else None
            kind = {"mod": "modified", "del": "removed", "add": "added"}[tag]
            changes.append({"kind": kind, "category": "text", "page": number,
                            "location": {"page": number}, "label": f"Page {number}",
                            "before": lb["t"] if lb else None, "after": lt["t"] if lt else None,
                            "base_bbox": lb.get("bb") if lb else None, "target_bbox": lt.get("bb") if lt else None})
    notes = ["Compared the visible text of each page and where it sits. Changes to drawings, images or annotations are not "
             "detected here; compare the pages side by side to inspect them."]
    return _finish(changes, notes)


# ------------------------------------------------------------------ XLSX
def _cell_key(address: str) -> tuple[int, int]:
    m = re.fullmatch(r"([A-Z]+)(\d+)", address)
    if not m:
        return (0, 0)
    col = 0
    for ch in m.group(1):
        col = col * 26 + ord(ch) - 64
    return (int(m.group(2)), col)


def _style_diff(a: dict[str, Any] | None, b: dict[str, Any] | None) -> str | None:
    a, b = a or {}, b or {}
    keys = sorted({k for k in set(a) | set(b)})
    parts = [f"{k}: {a.get(k, '—')} → {b.get(k, '—')}" for k in keys if a.get(k) != b.get(k)]
    return "; ".join(parts[:6]) if parts else None


def diff_xlsx(base: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    sb, st = base.get("sheets", {}), target.get("sheets", {})
    changes: list[dict[str, Any]] = []
    for name in list(sb) + [n for n in st if n not in sb]:
        if name not in st:
            changes.append({"kind": "removed", "category": "structure", "location": {"sheet": name}, "label": f"Sheet {name}",
                            "before": "worksheet present", "after": None})
            continue
        if name not in sb:
            changes.append({"kind": "added", "category": "structure", "location": {"sheet": name}, "label": f"Sheet {name}",
                            "before": None, "after": "worksheet present"})
            continue
        a, b = sb[name], st[name]
        cells_a, cells_b = a.get("cells", {}), b.get("cells", {})
        for address in sorted(set(cells_a) | set(cells_b), key=_cell_key):
            ca, cb = cells_a.get(address), cells_b.get(address)
            va = ca["v"] if ca else ""
            vb = cb["v"] if cb else ""
            ta, tb = (ca or {}).get("t"), (cb or {}).get("t")
            is_formula = ta == "formula" or tb == "formula"
            if (va, ta) != (vb, tb) and not (va == vb and not is_formula and {ta, tb} <= {"null", None, "string"}):
                kind = "added" if va in ("", None) and ta in (None, "null") else "removed" if vb in ("", None) and tb in (None, "null") else "modified"
                changes.append({"kind": kind, "category": "formula" if is_formula else "value",
                                "location": {"sheet": name, "cell": address}, "label": f"{name}!{address}",
                                "before": va if ca else None, "after": vb if cb else None})
            sa, sbb = a.get("styles", {}).get(address), b.get("styles", {}).get(address)
            if sa != sbb:
                description = _style_diff(sa, sbb)
                if description:
                    changes.append({"kind": "modified", "category": "format", "location": {"sheet": name, "cell": address},
                                    "label": f"{name}!{address}", "before": None, "after": None, "detail": description})
        ma, mb = {tuple(x) for x in a.get("merges", [])}, {tuple(x) for x in b.get("merges", [])}
        for rng in sorted(ma - mb):
            changes.append({"kind": "removed", "category": "structure", "location": {"sheet": name}, "label": f"{name} merged range",
                            "before": _rng(rng), "after": None})
        for rng in sorted(mb - ma):
            changes.append({"kind": "added", "category": "structure", "location": {"sheet": name}, "label": f"{name} merged range",
                            "before": None, "after": _rng(rng)})
        ia, ib = a.get("images", []), b.get("images", [])
        if sorted(ia) != sorted(ib):
            changes.append({"kind": "modified", "category": "image", "location": {"sheet": name}, "label": f"{name} pictures",
                            "before": f"{len(ia)} picture(s)", "after": f"{len(ib)} picture(s)"})
        if a.get("cols") != b.get("cols") or a.get("rows") != b.get("rows"):
            changes.append({"kind": "modified", "category": "format", "location": {"sheet": name}, "label": f"{name} row/column sizes",
                            "before": None, "after": None, "detail": "column widths or row heights differ"})
    notes = ["Compared sheet names, cell values and formulas, resolved cell formatting, merged ranges, pictures and sizes. "
             "Calculated values of formulas are not recomputed."]
    return _finish(changes, notes)


def _rng(r: tuple[int, ...]) -> str:
    def col(n: int) -> str:
        s = ""
        while n:
            n, rem = divmod(n - 1, 26)
            s = chr(65 + rem) + s
        return s
    r1, c1, r2, c2 = r
    return f"{col(c1)}{r1}:{col(c2)}{r2}"


# ------------------------------------------------------------------ DOCX
def _block_label(block: dict[str, Any], number: int) -> str:
    t = block.get("table")
    if t:
        return f"Table {t['index'] + 1}, row {t['row'] + 1}, column {t['col'] + 1}"
    kind = block.get("kind", "paragraph")
    if kind in {"header", "footer"}:
        return f"{kind.capitalize()}"
    return f"Paragraph {number}"


def _props_diff(a: dict[str, Any], b: dict[str, Any]) -> str:
    keys = sorted(set(a) | set(b))
    return "; ".join(f"{k}: {a.get(k, 'default')} → {b.get(k, 'default')}" for k in keys if a.get(k) != b.get(k)) or "paragraph or run formatting differs"


def diff_docx(base: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    bb, bt = base.get("blocks", []), target.get("blocks", [])
    changes: list[dict[str, Any]] = []
    for tag, i, j in _align([b["text"] for b in bb], [b["text"] for b in bt]):
        lb = bb[i] if i is not None else None
        lt = bt[j] if j is not None else None
        ref = lt or lb
        number = ((j if j is not None else i) or 0) + 1
        loc = {"block": ref["id"], "kind": ref.get("kind")}
        if ref.get("table"):
            loc["table"] = ref["table"]
        if tag == "same":
            if lb.get("fmt") != lt.get("fmt"):
                changes.append({"kind": "modified", "category": "format", "location": loc, "label": _block_label(lt, number),
                                "before": lb["text"], "after": lt["text"], "detail": _props_diff(lb.get("props", {}), lt.get("props", {}))})
            continue
        kind = {"mod": "modified", "del": "removed", "add": "added"}[tag]
        changes.append({"kind": kind, "category": "text", "location": loc, "label": _block_label(ref, number),
                        "before": lb["text"] if lb else None, "after": lt["text"] if lt else None})
    ma = {m["name"]: m["sha256"] for m in base.get("media", [])}
    mt = {m["name"]: m["sha256"] for m in target.get("media", [])}
    shas_a, shas_t = set(ma.values()), set(mt.values())
    for name in sorted(set(ma) | set(mt)):
        if name not in mt and ma[name] not in shas_t:
            changes.append({"kind": "removed", "category": "image", "location": {"media": name}, "label": f"Image {name}", "before": name, "after": None})
        elif name not in ma and mt[name] not in shas_a:
            changes.append({"kind": "added", "category": "image", "location": {"media": name}, "label": f"Image {name}", "before": None, "after": name})
        elif name in ma and name in mt and ma[name] != mt[name]:
            changes.append({"kind": "modified", "category": "image", "location": {"media": name}, "label": f"Image {name}", "before": name, "after": name})
    notes = ["Compared paragraph and table text, paragraph/run formatting and embedded images (by content hash). "
             "The position of images and shapes on the page is not compared; use the page view to inspect layout."]
    return _finish(changes, notes)


# ------------------------------------------------------------------ XLSX (complete, streamed from the windowed indexes)
FORMULA_REASONS = {"formula_cell", "array_formula"}


def _ordered_cells(conn, ) -> Any:
    return conn.execute("SELECT r,c,v,t,s,rs FROM cells ORDER BY r,c")


def _addr(r: int, c: int) -> str:
    col = ""
    n = c
    while n:
        n, rem = divmod(n - 1, 26)
        col = chr(65 + rem) + col
    return f"{col}{r}"


def diff_xlsx_indexed(meta_a: dict[str, Any], meta_b: dict[str, Any], open_a, open_b) -> dict[str, Any]:
    """Complete workbook comparison. `open_a(i)` / `open_b(i)` return a sqlite connection of the sheet index. Cells are walked as two
    ordered streams (row, column), so every cell of both workbooks is examined exactly once and nothing is dropped or invented."""
    styles_a, styles_b = meta_a.get("styles", []), meta_b.get("styles", [])
    sheets_a = {s["name"]: s for s in meta_a["sheets"]}
    sheets_b = {s["name"]: s for s in meta_b["sheets"]}
    changes = _BoundedChanges()
    examined = 0
    for name in list(sheets_a) + [n for n in sheets_b if n not in sheets_a]:
        if name not in sheets_b:
            changes.append({"kind": "removed", "category": "structure", "location": {"sheet": name}, "label": f"Sheet {name}", "before": "worksheet present", "after": None})
            continue
        if name not in sheets_a:
            changes.append({"kind": "added", "category": "structure", "location": {"sheet": name}, "label": f"Sheet {name}", "before": None, "after": "worksheet present"})
            continue
        a, b = sheets_a[name], sheets_b[name]
        ca_conn, cb_conn = open_a(a["index"]), open_b(b["index"])
        try:
            ia, ib = iter(_ordered_cells(ca_conn)), iter(_ordered_cells(cb_conn))
            ra, rb = next(ia, None), next(ib, None)
            while ra is not None or rb is not None:
                key_a = (ra[0], ra[1]) if ra is not None else None
                key_b = (rb[0], rb[1]) if rb is not None else None
                if key_b is None or (key_a is not None and key_a < key_b):
                    cell_a, cell_b, rc = ra, None, key_a
                    ra = next(ia, None)
                elif key_a is None or key_b < key_a:
                    cell_a, cell_b, rc = None, rb, key_b
                    rb = next(ib, None)
                else:
                    cell_a, cell_b, rc = ra, rb, key_a
                    ra, rb = next(ia, None), next(ib, None)
                examined += 1
                va = cell_a[2] if cell_a else ""
                vb = cell_b[2] if cell_b else ""
                ta = ("formula" if (cell_a and cell_a[5] in FORMULA_REASONS) else (cell_a[3] if cell_a else None))
                tb = ("formula" if (cell_b and cell_b[5] in FORMULA_REASONS) else (cell_b[3] if cell_b else None))
                is_formula = ta == "formula" or tb == "formula"
                address = _addr(*rc)
                if (va, ta) != (vb, tb) and not (va == vb and not is_formula and {ta, tb} <= {"null", None, "string"}):
                    kind = "added" if va in ("", None) and ta in (None, "null") else "removed" if vb in ("", None) and tb in (None, "null") else "modified"
                    changes.append({"kind": kind, "category": "formula" if is_formula else "value", "location": {"sheet": name, "cell": address}, "rc": list(rc),
                                    "label": f"{name}!{address}", "before": va if cell_a else None, "after": vb if cell_b else None})
                sa = styles_a[cell_a[4]] if cell_a and cell_a[4] is not None and 0 <= cell_a[4] < len(styles_a) else None
                sb = styles_b[cell_b[4]] if cell_b and cell_b[4] is not None and 0 <= cell_b[4] < len(styles_b) else None
                if sa != sb:
                    description = _style_diff(sa, sb)
                    if description:
                        changes.append({"kind": "modified", "category": "format", "location": {"sheet": name, "cell": address}, "rc": list(rc),
                                        "label": f"{name}!{address}", "before": None, "after": None, "detail": description})
        finally:
            ca_conn.close(); cb_conn.close()
        ma, mb = {tuple(x) for x in a.get("merges", [])}, {tuple(x) for x in b.get("merges", [])}
        for rng in sorted(ma - mb):
            changes.append({"kind": "removed", "category": "structure", "location": {"sheet": name}, "label": f"{name} merged range", "before": _rng(rng), "after": None})
        for rng in sorted(mb - ma):
            changes.append({"kind": "added", "category": "structure", "location": {"sheet": name}, "label": f"{name} merged range", "before": None, "after": _rng(rng)})
        pics = lambda m: sorted(f"{hashlib.sha256(str(i.get('src', '')).encode()).hexdigest()[:16]}@{(i.get('from') or {}).get('row')}:{(i.get('from') or {}).get('col')}" for i in m.get("images", []))  # noqa: E731
        if pics(a) != pics(b):
            changes.append({"kind": "modified", "category": "image", "location": {"sheet": name}, "label": f"{name} pictures",
                            "before": f"{len(a.get('images', []))} picture(s)", "after": f"{len(b.get('images', []))} picture(s)"})
        if a.get("cols") != b.get("cols") or a.get("rows") != b.get("rows") or a.get("freeze") != b.get("freeze") or bool(a.get("hidden")) != bool(b.get("hidden")):
            changes.append({"kind": "modified", "category": "format", "location": {"sheet": name}, "label": f"{name} row/column sizes and panes",
                            "before": None, "after": None, "detail": "column widths, row heights, frozen panes or visibility differ"})
    result = _finish(changes, ["Compared every cell of every worksheet (values, formulas, resolved formatting), merged ranges, pictures, sizes and "
                               "frozen panes. Calculated values of formulas are not recomputed."])
    result["examined_cells"] = examined
    return result


DIFFERS = {"pdf": diff_pdf, "xlsx": diff_xlsx, "docx": diff_docx}
