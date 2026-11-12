"""Display layout of an XLSX: merged ranges, column widths, row heights, resolved cell styles and anchored pictures.

Read-only. Runs inside the sandboxed parser (never in the API process). It does not evaluate formulas and never
fetches external content: pictures come only from the package's own media parts and are size-capped.
The editor uses this to look like the real workbook; editing still goes through the verified XLSX adapter.
"""
from __future__ import annotations

import base64
import io
import posixpath
import re
import zipfile
from typing import Any

from lxml import etree
from openpyxl.styles.colors import COLOR_INDEX
from openpyxl.utils.cell import get_column_letter, range_boundaries

M = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": M, "r": R, "xdr": XDR, "a": A, "pr": PR}
MAX_PART = 8 * 1024 * 1024
MAX_IMAGE = 600 * 1024
MAX_ROWS, MAX_COLS = 500, 40
MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif"}
THEME_ORDER = ["lt1", "dk1", "lt2", "dk2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "hlink", "folHlink"]
DEFAULT_THEME = ["FFFFFF", "000000", "EEECE1", "1F497D", "4F81BD", "C0504D", "9BBB59", "8064A2", "4BACC6", "F79646", "0000FF", "800080"]


def _parse(data: bytes):
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, remove_comments=True)
    return etree.fromstring(data, parser)


def _read(zf: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_PART:
        return None
    return zf.read(name)


def _rels(zf: zipfile.ZipFile, part: str) -> dict[str, str]:
    base, leaf = posixpath.split(part)
    raw = _read(zf, posixpath.join(base, "_rels", leaf + ".rels"))
    out: dict[str, str] = {}
    if raw:
        for rel in _parse(raw).iterfind("pr:Relationship", NS):
            if rel.get("TargetMode") == "External":
                continue
            target = rel.get("Target", "")
            out[rel.get("Id", "")] = posixpath.normpath(target[1:] if target.startswith("/") else posixpath.join(base, target))
    return out


def _theme(zf: zipfile.ZipFile) -> list[str]:
    raw = _read(zf, "xl/theme/theme1.xml")
    if not raw:
        return DEFAULT_THEME
    colors = {}
    try:
        for name in THEME_ORDER:
            el = _parse(raw).find(f".//a:clrScheme/a:{name}", NS)
            if el is not None and len(el):
                child = el[0]
                raw_val = child.get("lastClr") if etree.QName(child).localname == "sysClr" else child.get("val")
                colors[name] = raw_val if raw_val and re.fullmatch(r"[0-9A-Fa-f]{6}", raw_val) else "000000"
    except etree.XMLSyntaxError:
        return DEFAULT_THEME
    return [colors.get(n, d) for n, d in zip(THEME_ORDER, DEFAULT_THEME)]


def _tint(rgb: str, tint: float) -> str:
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", rgb):
        return "000000"
    r, g, b = (int(rgb[i:i + 2], 16) for i in (0, 2, 4))
    f = (lambda c: round(c * (1 + tint))) if tint < 0 else (lambda c: round(c + (255 - c) * tint))
    return "".join(f"{max(0, min(255, f(c))):02X}" for c in (r, g, b))


def _color(el, theme: list[str]) -> str | None:
    if el is None:
        return None
    if el.get("rgb"):
        return "#" + el.get("rgb")[-6:]
    if el.get("theme") is not None:
        try:
            base = theme[int(el.get("theme"))]
        except (ValueError, IndexError):
            return None
        return "#" + (_tint(base, float(el.get("tint", 0))) if el.get("tint") else base)
    if el.get("indexed") is not None:
        try:
            return "#" + COLOR_INDEX[int(el.get("indexed"))][-6:]
        except (ValueError, IndexError):
            return None
    return None


def _styles(zf: zipfile.ZipFile, theme: list[str]) -> list[dict[str, Any]]:
    raw = _read(zf, "xl/styles.xml")
    if not raw:
        return []
    root = _parse(raw)
    fonts, fills, borders = [], [], []
    for f in root.iterfind("m:fonts/m:font", NS):
        d: dict[str, Any] = {}
        if f.find("m:b", NS) is not None and f.find("m:b", NS).get("val") != "0":
            d["b"] = 1
        if f.find("m:i", NS) is not None and f.find("m:i", NS).get("val") != "0":
            d["i"] = 1
        if f.find("m:u", NS) is not None:
            d["u"] = 1
        sz = f.find("m:sz", NS)
        if sz is not None and sz.get("val"):
            d["sz"] = float(sz.get("val"))
        c = _color(f.find("m:color", NS), theme)
        if c and c != "#000000":
            d["c"] = c
        fonts.append(d)
    for f in root.iterfind("m:fills/m:fill", NS):
        pf = f.find("m:patternFill", NS)
        bg = None
        if pf is not None and pf.get("patternType") == "solid":
            bg = _color(pf.find("m:fgColor", NS), theme)
            if bg == "#FFFFFF" or bg is None:
                bg = bg if bg and bg != "#FFFFFF" else None
        fills.append(bg)
    for b in root.iterfind("m:borders/m:border", NS):
        borders.append({k: 1 for k, tag in (("bl", "left"), ("br", "right"), ("bt", "top"), ("bb", "bottom"))
                        if b.find(f"m:{tag}", NS) is not None and b.find(f"m:{tag}", NS).get("style")})
    out = []
    for xf in root.iterfind("m:cellXfs/m:xf", NS):
        d: dict[str, Any] = {}
        font = fonts[int(xf.get("fontId", 0))] if int(xf.get("fontId", 0)) < len(fonts) else {}
        d.update(font)
        fill = fills[int(xf.get("fillId", 0))] if int(xf.get("fillId", 0)) < len(fills) else None
        if fill:
            d["bg"] = fill
        border = borders[int(xf.get("borderId", 0))] if int(xf.get("borderId", 0)) < len(borders) else {}
        d.update(border)
        al = xf.find("m:alignment", NS)
        if al is not None:
            if al.get("horizontal") in ("left", "center", "right", "justify", "centerContinuous"):
                d["ha"] = "center" if al.get("horizontal") == "centerContinuous" else al.get("horizontal")
            if al.get("vertical") in ("top", "center", "bottom"):
                d["va"] = al.get("vertical")
            if al.get("wrapText") == "1":
                d["wrap"] = 1
        out.append(d)
    return out


def _picture(zf: zipfile.ZipFile, drawing: str) -> list[dict[str, Any]]:
    raw = _read(zf, drawing)
    if not raw:
        return []
    rels = _rels(zf, drawing)
    out = []
    for anchor in _parse(raw):
        kind = etree.QName(anchor).localname
        if kind not in ("twoCellAnchor", "oneCellAnchor", "absoluteAnchor"):
            continue
        blip = anchor.find(".//a:blip", NS)
        if blip is None:
            continue
        target = rels.get(blip.get(f"{{{R}}}embed", ""))
        ext = (target or "").rsplit(".", 1)[-1].lower()
        data = _read(zf, target) if target else None
        if not data or ext not in MIME or len(data) > MAX_IMAGE:
            continue

        def pos(tag: str):
            el = anchor.find(f"xdr:{tag}", NS)
            if el is None:
                return None
            g = lambda n: int(el.findtext(f"xdr:{n}", "0", NS) or 0)
            return {"col": g("col"), "row": g("row"), "dx": g("colOff"), "dy": g("rowOff")}

        item: dict[str, Any] = {"src": f"data:{MIME[ext]};base64,{base64.b64encode(data).decode()}", "from": pos("from")}
        if kind == "twoCellAnchor":
            item["to"] = pos("to")
        else:
            e = anchor.find("xdr:ext", NS)
            if e is not None:
                item["ext"] = {"cx": int(e.get("cx", 0)), "cy": int(e.get("cy", 0))}
        out.append(item)
    return out


def extract(data: bytes) -> dict[str, Any] | None:
    """Per-sheet layout, or None when the package can't be read safely."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        wb_raw = _read(zf, "xl/workbook.xml")
        if wb_raw is None:
            return None
        theme = _theme(zf)
        styles = _styles(zf, theme)
        wb_rels = _rels(zf, "xl/workbook.xml")
        sheets = []
        for s in _parse(wb_raw).iterfind("m:sheets/m:sheet", NS):
            part = wb_rels.get(s.get(f"{{{R}}}id", ""))
            raw = _read(zf, part) if part else None
            if raw is None:
                sheets.append({"name": s.get("name", ""), "merges": [], "cols": {}, "rows": {}, "cells": {}, "images": []})
                continue
            root = _parse(raw)
            fmt = root.find("m:sheetFormatPr", NS)
            default_col = float(fmt.get("defaultColWidth", 0) or 0) if fmt is not None else 0.0
            default_row = float(fmt.get("defaultRowHeight", 15) or 15) if fmt is not None else 15.0
            cols: dict[str, float] = {}
            for c in root.iterfind("m:cols/m:col", NS):
                lo, hi = int(c.get("min", 0)), int(c.get("max", 0))
                width = float(c.get("width", 0) or 0)
                hidden = c.get("hidden") in ("1", "true")
                for i in range(max(lo, 1), min(hi, MAX_COLS) + 1):
                    cols[str(i)] = 0.0 if hidden else width
            rows: dict[str, float] = {}
            cell_style: dict[str, int] = {}
            for r in root.iterfind("m:sheetData/m:row", NS):
                n = int(r.get("r", 0))
                if n > MAX_ROWS:
                    break
                if r.get("ht") and r.get("customHeight") in ("1", "true", None):
                    rows[str(n)] = 0.0 if r.get("hidden") in ("1", "true") else float(r.get("ht"))
                for c in r.iterfind("m:c", NS):
                    if c.get("s") not in (None, "0"):
                        cell_style[c.get("r", "")] = int(c.get("s"))
            merges = []
            for mc in root.iterfind("m:mergeCells/m:mergeCell", NS):
                try:
                    c1, r1, c2, r2 = range_boundaries(mc.get("ref", ""))
                except (ValueError, TypeError):
                    continue
                if r1 <= MAX_ROWS and c1 <= MAX_COLS:
                    merges.append([r1, c1, min(r2, MAX_ROWS), min(c2, MAX_COLS)])
            images: list[dict[str, Any]] = []
            drawing = root.find("m:drawing", NS)
            if drawing is not None:
                target = _rels(zf, part).get(drawing.get(f"{{{R}}}id", ""))
                if target:
                    images = _picture(zf, target)
            used = {k: v for k, v in cell_style.items() if v < len(styles) and styles[v]}
            sheets.append({"name": s.get("name", ""), "default_col": default_col, "default_row": default_row, "merges": merges,
                           "cols": cols, "rows": rows, "cells": {k: v for k, v in used.items()}, "images": images})
        return {"styles": styles, "sheets": sheets}
    except (zipfile.BadZipFile, etree.XMLSyntaxError, KeyError, ValueError, OSError):
        return None
