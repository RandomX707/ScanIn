"""Genuine content-stream text editing for ordinary (non-AcroForm) PDFs.

Local, dependency-light (pypdf + pypdfium2 already in the project).  An edit rewrites the text-showing operators
of the selected run so the ORIGINAL glyphs are removed from the content stream and the replacement is shown with the
SAME embedded font (re-encoded).  No annotations, no overlays, no white-out rectangles.

Scope of this module (see docs/pdf-editing-engine.md for the limits that are reported to users):
  * glyph geometry from a real content-stream interpreter (CTM, Tm, Tc/Tw/Tz/Ts, Td/TD/T*, Tj/TJ/'/"), recursing
    into Form XObjects;
  * Type0/Identity-H (2-byte) and simple (1-byte) fonts, widths from /W,/DW,/Widths, text from /ToUnicode or the
    declared encoding;
  * a replacement is only produced when every new character exists in the original (possibly subset) font;
    otherwise the edit is refused with a precise reason (no silent font substitution);
  * fit analysis: the new advance width must fit the original run box plus the free space up to the next object.
"""
from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass, field
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, ByteStringObject, ContentStream, DecodedStreamObject, DictionaryObject,
                           FloatObject, NameObject, NumberObject, TextStringObject)

IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
SPACE_GAP_EM = 0.18        # gap larger than this (in em) between glyphs is treated as a word space
RUN_BREAK_EM = 1.2         # gap larger than this starts a new run (separate table cell / column)
BASELINE_TOL = 0.6
FIT_MIN_FACTOR = 0.85      # never shrink or condense below 85% of the original
FIT_MAX_SPACING_EM = 0.04  # never tighten letter spacing by more than 0.04 em per character
NEIGHBOUR_GAP = 2.0        # clear space kept between an edited value and the next text object
FIT_STRATEGIES = ("size", "scale", "spacing", "expand")


def mul(a, b):
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


def apply(m, x, y):
    return (x * m[0] + y * m[2] + m[4], x * m[1] + y * m[3] + m[5])


def _num(x) -> float:
    return float(x)


class EditRefused(Exception):
    """The requested edit cannot be done safely.  `code` is stable, `message` is user-facing."""

    def __init__(self, code: str, message: str, extra: dict[str, Any] | None = None):
        self.code, self.message, self.extra = code, message, extra or {}
        super().__init__(code)


# ───────────────────────────── fonts ─────────────────────────────

_STD14 = {"Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique", "Times-Roman", "Times-Bold",
          "Times-Italic", "Times-BoldItalic", "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique"}


def _standard14(base: str) -> str | None:
    name = base.split("+")[-1]
    return name if name in _STD14 else None


def _std_width(name: str, ch: str) -> float:
    from reportlab.pdfbase import pdfmetrics          # exact AFM metrics for the 12 text base-14 fonts
    try:
        return float(pdfmetrics.stringWidth(ch, name, 1000))
    except Exception:  # pragma: no cover
        return 500.0


_BF = re.compile(rb"beginbfchar(.*?)endbfchar", re.S)
_BR = re.compile(rb"beginbfrange(.*?)endbfrange", re.S)
_HEX = re.compile(rb"<([0-9A-Fa-f\s]*)>")


def _u16(h: bytes) -> str:
    raw = bytes.fromhex(h.decode().replace(" ", "").replace("\n", "").replace("\r", ""))
    return raw.decode("utf-16-be", "replace") if raw else ""


def parse_tounicode(data: bytes) -> dict[int, str]:
    out: dict[int, str] = {}
    for block in _BF.findall(data):
        toks = _HEX.findall(block)
        for i in range(0, len(toks) - 1, 2):
            out[int(toks[i].decode().replace(" ", "") or "0", 16)] = _u16(toks[i + 1])
    for block in _BR.findall(data):
        for m in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f\s]*>)", block):
            lo, hi = int(m.group(1), 16), int(m.group(2), 16)
            if hi - lo > 0xFFFF:
                continue
            dst = m.group(3)
            if dst.startswith(b"["):
                for k, t in enumerate(_HEX.findall(dst)):
                    out[lo + k] = _u16(t)
            else:
                base = _u16(_HEX.findall(dst)[0])
                for k in range(hi - lo + 1):
                    out[lo + k] = (base[:-1] + chr(ord(base[-1]) + k)) if base else ""
    return out


class _GlyphProgram:
    """Does the embedded TrueType program really contain an outline for a code? (None = can't tell)"""

    def __init__(self, font: DictionaryObject):
        self.tt = None
        self.cmap = None
        self.error = False
        try:
            desc = font["/DescendantFonts"][0].get_object() if str(font.get("/Subtype")) == "/Type0" else font
            fd = desc.get("/FontDescriptor")
            ff = fd.get_object().get("/FontFile2") if fd is not None else None
            if ff is not None:
                from fontTools.ttLib import TTFont
                self.tt = TTFont(io.BytesIO(ff.get_object().get_data()), lazy=True)
                self.order = self.tt.getGlyphOrder()
                self.glyf = self.tt["glyf"]
                table = self.tt["cmap"]
                self.cmap = (table.getcmap(3, 1) or table.getcmap(1, 0) or table.getcmap(3, 0))
                self.cmap = self.cmap.cmap if self.cmap is not None else None
        except Exception:
            self.tt, self.error = None, True

    def has(self, code: int, ch: str, type0: bool) -> bool | None:
        if self.tt is None:
            return None
        try:
            if type0:
                gid = code                                     # Identity CIDToGIDMap
            else:
                if not self.cmap:
                    return None
                name = self.cmap.get(ord(ch)) or self.cmap.get(0xF000 + code) or self.cmap.get(code)
                if name is None:
                    return False
                gid = self.order.index(name)
            if gid >= len(self.order):
                return False
            return self.glyf[self.order[gid]].numberOfContours != 0
        except Exception:
            return None


class FontInfo:
    def __init__(self, font: DictionaryObject, key: str):
        self.key = key
        self.base = str(font.get("/BaseFont", "")).lstrip("/")
        self.subtype = str(font.get("/Subtype", ""))
        self.is_type0 = self.subtype == "/Type0"
        self.code_len = 2 if self.is_type0 else 1
        self.supported = True
        self.unsupported_reason = ""
        self.subset = bool(re.match(r"^[A-Z]{6}\+", self.base))
        self.estimated_widths = False
        self.to_uni: dict[int, str] = {}
        self.used: dict[str, int] = {}          # char -> a code seen in real page content (its glyph surely exists)
        self._program = _GlyphProgram(font)
        tu = font.get("/ToUnicode")
        if tu is not None:
            try:
                self.to_uni = parse_tounicode(tu.get_object().get_data())
            except Exception:
                self.to_uni = {}
        self.dw = 1000.0
        self.w: dict[int, float] = {}
        self.first = 0
        self.widths: list[float] = []
        self.missing = 0.0
        if self.is_type0:
            enc = font.get("/Encoding")
            enc_name = str(enc) if isinstance(enc, str) or hasattr(enc, "startswith") else ""
            if not str(enc_name).startswith("/Identity"):
                self.supported, self.unsupported_reason = False, "unsupported_cmap"
            desc = font["/DescendantFonts"][0].get_object()
            self.dw = float(desc.get("/DW", 1000))
            self._read_cid_widths(desc.get("/W"))
        else:
            self.first = int(font.get("/FirstChar", 0))
            wl = font.get("/Widths")
            self.std_name = _standard14(self.base) if wl is None else None
            if wl is not None:
                self.widths = [float(x) for x in wl.get_object()]
            elif self.std_name is None:
                self.estimated_widths = True
            fd = font.get("/FontDescriptor")
            if fd is not None:
                self.missing = float(fd.get_object().get("/MissingWidth", 0))
            self._simple_unicode(font)

    def _read_cid_widths(self, arr):
        if arr is None:
            return
        arr = [x.get_object() for x in arr.get_object()]
        i = 0
        while i < len(arr):
            c = int(arr[i])
            nxt = arr[i + 1] if i + 1 < len(arr) else None
            if isinstance(nxt, list) or isinstance(nxt, ArrayObject):
                for k, w in enumerate(nxt):
                    self.w[c + k] = float(w.get_object() if hasattr(w, "get_object") else w)
                i += 2
            else:
                c2, w = int(arr[i + 1]), float(arr[i + 2])
                for k in range(c, c2 + 1):
                    self.w[k] = w
                i += 3

    def _simple_unicode(self, font):
        if self.to_uni:
            return
        enc = font.get("/Encoding")
        base = "cp1252"
        diffs: dict[int, str] = {}
        if enc is not None:
            enc = enc.get_object()
            if isinstance(enc, DictionaryObject):
                be = str(enc.get("/BaseEncoding", "/WinAnsiEncoding"))
                base = "mac_roman" if "Mac" in be else "cp1252"
                code = 0
                for item in enc.get("/Differences", []):
                    item = item.get_object()
                    if isinstance(item, (int, float)) or isinstance(item, NumberObject):
                        code = int(item)
                    else:
                        diffs[code] = str(item)
                        code += 1
            else:
                base = "mac_roman" if "Mac" in str(enc) else "cp1252"
        try:
            from pypdf._codecs import adobe_glyphs
        except Exception:  # pragma: no cover
            adobe_glyphs = {}
        for code in range(32, 256):
            if code in diffs:
                name = diffs[code].lstrip("/")
                ch = adobe_glyphs.get(name) or (chr(int(name[3:], 16)) if re.fullmatch(r"uni[0-9A-Fa-f]{4}", name) else None)
                if ch:
                    self.to_uni[code] = ch
                continue
            try:
                self.to_uni[code] = bytes([code]).decode(base)
            except Exception:
                pass

    def width(self, code: int) -> float:
        if self.is_type0:
            return self.w.get(code, self.dw)
        if getattr(self, "std_name", None):
            ch = self.to_uni.get(code)
            if not ch:
                return 500.0
            return _std_width(self.std_name, ch)
        i = code - self.first
        if 0 <= i < len(self.widths):
            return self.widths[i] or self.missing
        return self.missing if self.missing else 500.0

    def decode(self, raw: bytes) -> list[tuple[int, bytes]]:
        n = self.code_len
        return [(int.from_bytes(raw[i:i + n], "big"), raw[i:i + n]) for i in range(0, len(raw) - n + 1, n)]

    @property
    def reverse(self) -> dict[str, int]:
        if not hasattr(self, "_rev"):
            rev: dict[str, int] = {}
            for code, ch in sorted(self.to_uni.items()):
                if len(ch) == 1 and ch not in rev:
                    rev[ch] = code
            self._rev = rev
        return self._rev

    def code_for(self, ch: str) -> int | None:
        return self.used.get(ch, self.reverse.get(ch))

    def has_glyph(self, ch: str) -> bool | None:
        """True/False when the embedded font program can be inspected, None when it can't (unknown)."""
        if ch in self.used:
            return True
        code = self.reverse.get(ch)
        if code is None:
            return False
        if ch.isspace():
            return True
        return self._program.has(code, ch, self.is_type0)

    def encode_char(self, ch: str) -> bytes | None:
        code = self.reverse.get(ch)
        return None if code is None else code.to_bytes(self.code_len, "big")


# ───────────────────────────── runs ─────────────────────────────

@dataclass
class Glyph:
    code: bytes
    ch: str
    x0: float
    x1: float
    y: float
    em: float          # advance of one em in user space (effective font size)
    cid: int = 0


@dataclass
class Run:
    id: str
    page: int
    container: tuple[str, ...]
    text: str
    bbox: tuple[float, float, float, float]
    x0: float
    x1: float
    y: float
    size: float
    font_key: str
    font_name: str
    i0: int
    i1: int
    glyphs: list[Glyph] = field(repr=False, default_factory=list)
    tj_start: int = 0
    tf: tuple[str, float] = ("", 0.0)
    xem: float = 0.0          # user-space units per em horizontally (includes Tz and the CTM)
    tc_user: float = 0.0      # extra advance per glyph from Tc (user units)
    td_indices: list[int] = field(repr=False, default_factory=list)
    tc_raw: float = 0.0       # Tc / Tz in effect for the run (restored after a fitted edit)
    th_raw: float = 1.0
    rot: float = 0.0          # |sin| of the text direction: anything above ~0 is rotated / transformed
    clip: Any = None          # None, a (x0, y0, x1, y1) box, or "unknown" when a non-rectangular clip is active

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "page": self.page + 1, "text": self.text, "bbox": [round(v, 2) for v in self.bbox],
                "font": self.font_name, "size": round(self.size, 2)}


class _Container:
    """A content stream (page or form XObject) plus how to write it back."""

    def __init__(self, obj, resources, ctm, path, reader, owner_page):
        self.obj, self.resources, self.ctm, self.path, self.owner_page = obj, resources, ctm, path, owner_page
        self.cs = ContentStream(obj.get_contents() if hasattr(obj, "get_contents") and "/Contents" in obj else obj, reader)
        self.ops = self.cs.operations


class PdfTextEngine:
    def __init__(self, data: bytes):
        self.reader = PdfReader(io.BytesIO(data))
        if self.reader.is_encrypted:
            raise EditRefused("encrypted", "This PDF is encrypted, so its text can't be changed.")
        self.writer = PdfWriter(clone_from=self.reader)
        self._fonts: dict[int, FontInfo] = {}
        self._containers: dict[tuple[int, tuple[str, ...]], _Container] = {}
        self._runs: dict[int, list[Run]] = {}
        self._owned: set[int] = set()
        self._counter = 0
        self._vlines: dict[int, list[tuple[float, float, float]]] = {}   # page -> (x, y0, y1) table borders / rules
        self._hlines: dict[int, list[tuple[float, float, float]]] = {}   # page -> (y, x0, x1)

    @property
    def page_count(self) -> int:
        return len(self.writer.pages)

    # -- font cache
    def _font(self, res: DictionaryObject | None, name: str) -> FontInfo | None:
        fonts = (res or {}).get("/Font") if res is not None else None
        if fonts is None:
            return None
        fobj = fonts.get_object().get(name)
        if fobj is None:
            return None
        ref = getattr(fobj, "idnum", None) or id(fobj.get_object())
        fi = self._fonts.get(ref)
        if fi is None:
            fi = self._fonts[ref] = FontInfo(fobj.get_object(), name)
        return fi

    # -- interpretation
    def runs(self, page: int) -> list[Run]:
        if page in self._runs:
            return self._runs[page]
        pg = self.writer.pages[page]
        res = _page_resources(pg)
        out: list[Run] = []
        self._vlines[page], self._hlines[page] = [], []
        self._walk(page, pg, res, IDENT, (), out, depth=0)
        self._runs[page] = out
        return out

    def _walk(self, page: int, obj, resources, ctm0, path, out: list[Run], depth: int, clip0: Any = None):
        key = (page, path)
        cont = _Container(obj, resources, ctm0, path, self.reader, page)
        self._containers[key] = cont
        ops = cont.ops
        ctm, stack = ctm0, []
        font: FontInfo | None = None
        font_name = ""
        fs = 0.0
        tc = tw = rise = 0.0
        th = 1.0
        tm = tlm = IDENT
        tl = 0.0
        cur: list[Glyph] = []
        cur_meta: dict[str, Any] = {}
        glyph_ops: list[int] = []
        td_ops: list[int] = []
        cont.opcount = {}
        cont.actual = {}
        mc_stack: list[int | None] = []
        clip: Any = clip0
        subpaths: list[dict] = []
        pending_clip: Any = None
        vl, hl = self._vlines.setdefault(page, []), self._hlines.setdefault(page, [])

        def add_segment(a, b):
            dx, dy = b[0] - a[0], b[1] - a[1]
            if abs(dx) < 0.5 and abs(dy) > 0.5:
                vl.append((a[0], min(a[1], b[1]), max(a[1], b[1])))
            elif abs(dy) < 0.5 and abs(dx) > 0.5:
                hl.append((a[1], min(a[0], b[0]), max(a[0], b[0])))

        def paint(stroke: bool, fill: bool):
            for sp in subpaths:
                if sp["curved"] or len(sp["pts"]) < 2:
                    continue
                xs, ys = [q[0] for q in sp["pts"]], [q[1] for q in sp["pts"]]
                if sp["rect"]:
                    x0_, x1_, y0_, y1_ = min(xs), max(xs), min(ys), max(ys)
                    w_, h_ = x1_ - x0_, y1_ - y0_
                    if abs(sp["pts"][0][0] - sp["pts"][1][0]) > 1e-6 and abs(sp["pts"][0][1] - sp["pts"][1][1]) > 1e-6:
                        continue                                  # rotated rectangle: not a table rule
                    if fill and min(w_, h_) <= 2.0 and max(w_, h_) > 2.0:      # a rule drawn as a thin filled rectangle
                        if h_ >= w_:
                            vl.append(((x0_ + x1_) / 2, y0_, y1_))
                        else:
                            hl.append(((y0_ + y1_) / 2, x0_, x1_))
                    elif stroke and max(w_, h_) > 2.0:
                        add_segment((x0_, y0_), (x0_, y1_)); add_segment((x1_, y0_), (x1_, y1_))
                        add_segment((x0_, y0_), (x1_, y0_)); add_segment((x0_, y1_), (x1_, y1_))
                elif stroke:
                    for a, b in zip(sp["pts"], sp["pts"][1:]):
                        add_segment(a, b)

        def flush():
            nonlocal cur, cur_meta, glyph_ops, td_ops
            if cur:
                out.append(self._make_run(page, path, cur, cur_meta, glyph_ops, td_ops))
            cur, cur_meta, glyph_ops, td_ops = [], {}, [], []

        def show(raw: bytes, op_index: int, adjust: float | None = None):
            nonlocal tm, cur_meta
            if font is None:
                return
            for code, rawc in font.decode(raw):
                w = font.width(code)
                trm = mul((fs * th, 0, 0, fs, 0, rise), mul(tm, ctm))
                x0, y0 = apply(trm, 0, 0)
                x1, _ = apply(trm, w / 1000.0, 0)
                em = math.hypot(*apply(trm, 0, 1)) if False else math.hypot(trm[2], trm[3])
                ch = font.to_uni.get(code, "")
                if len(ch) == 1:
                    font.used.setdefault(ch, code)
                g = Glyph(rawc, ch, x0, x1, y0, em, code)
                if cur:
                    last = cur[-1]
                    gap = (g.x0 - last.x1)
                    same_line = abs(g.y - last.y) <= BASELINE_TOL
                    if (not same_line or abs(em - last.em) > 0.05 or gap > RUN_BREAK_EM * em or gap < -0.6 * em
                            or cur_meta["font"] is not font or cur_meta["container_op"] != "ok"):
                        flush()
                if not cur:
                    cur_meta = {"font": font, "container_op": "ok", "size": em, "tf": (font_name, fs), "tc_raw": tc, "th_raw": th,
                            "rot": abs(trm[1]) / (math.hypot(trm[0], trm[1]) or 1.0) if trm[0] > 0 else 1.0,
                            "clip": clip,
                            "xem": math.hypot(trm[0], trm[1]), "tc_user": (tc * math.hypot(trm[0], trm[1]) / fs) if fs else 0.0}
                cur.append(g)
                cont.opcount[op_index] = cont.opcount.get(op_index, 0) + 1
                cont.actual[op_index] = tuple(i for i in mc_stack if i is not None)
                if not glyph_ops or glyph_ops[-1] != op_index:
                    glyph_ops.append(op_index)
                tx = (w / 1000.0 * fs + tc + (tw if (len(rawc) == 1 and code == 32) else 0.0)) * th
                tm = mul((1, 0, 0, 1, tx, 0), tm)

        for idx, (operands, op) in enumerate(ops):
            o = op.decode() if isinstance(op, bytes) else op
            try:
                if o == "BDC":
                    props = operands[1] if len(operands) > 1 else None
                    if isinstance(props, (str, NameObject)) and resources is not None:      # named property list
                        props = ((resources.get("/Properties") or {}).get(str(props)) if resources.get("/Properties") else None)
                        props = props.get_object() if props is not None else None
                    has = isinstance(props, DictionaryObject) and ("/ActualText" in props)
                    mc_stack.append(idx if has else None)
                elif o == "BMC":
                    mc_stack.append(None)
                elif o == "EMC":
                    if mc_stack:
                        mc_stack.pop()
                elif o == "q":
                    stack.append((ctm, font, fs, tc, tw, rise, th, tl, font_name, clip))
                elif o == "Q":
                    if stack:
                        ctm, font, fs, tc, tw, rise, th, tl, font_name, clip = stack.pop()
                elif o == "cm":
                    ctm = mul(tuple(_num(x) for x in operands), ctm)
                elif o == "m":
                    subpaths.append({"pts": [apply(ctm, _num(operands[0]), _num(operands[1]))], "rect": False, "curved": False})
                elif o == "l":
                    if subpaths:
                        subpaths[-1]["pts"].append(apply(ctm, _num(operands[0]), _num(operands[1])))
                elif o in ("c", "v", "y"):
                    if subpaths:
                        subpaths[-1]["curved"] = True
                        subpaths[-1]["pts"].append(apply(ctm, _num(operands[-2]), _num(operands[-1])))
                elif o == "re":
                    rx, ry, rw, rh = (_num(v) for v in operands[:4])
                    c4 = [apply(ctm, rx, ry), apply(ctm, rx + rw, ry), apply(ctm, rx + rw, ry + rh), apply(ctm, rx, ry + rh)]
                    subpaths.append({"pts": c4 + [c4[0]], "rect": True, "curved": False})
                elif o == "h":
                    if subpaths and subpaths[-1]["pts"]:
                        subpaths[-1]["pts"].append(subpaths[-1]["pts"][0])
                elif o in ("S", "s", "f", "F", "f*", "B", "B*", "b", "b*"):
                    paint(o in ("S", "s", "B", "B*", "b", "b*"), o in ("f", "F", "f*", "B", "B*", "b", "b*"))
                    if pending_clip is not None:
                        clip, pending_clip = _merge_clip(clip, pending_clip), None
                    subpaths = []
                elif o in ("W", "W*"):
                    pending_clip = "unknown"
                    if len(subpaths) == 1 and not subpaths[0]["curved"] and len(subpaths[0]["pts"]) >= 4:
                        px = [q[0] for q in subpaths[0]["pts"]]; py = [q[1] for q in subpaths[0]["pts"]]
                        x_lo, x_hi, y_lo, y_hi = min(px), max(px), min(py), max(py)
                        # a rectangle (drawn with `re` or as an axis-aligned m/l/h path): every vertex sits on a corner of its bounding box
                        if all((abs(a - x_lo) < 1e-3 or abs(a - x_hi) < 1e-3) and (abs(b - y_lo) < 1e-3 or abs(b - y_hi) < 1e-3) for a, b in zip(px, py)):
                            pending_clip = (x_lo, y_lo, x_hi, y_hi)
                elif o == "n":
                    if pending_clip is not None:
                        clip, pending_clip = _merge_clip(clip, pending_clip), None
                    subpaths = []
                elif o == "BT":
                    flush()
                    tm = tlm = IDENT
                elif o == "ET":
                    flush()
                elif o == "Tf":
                    flush()
                    font = self._font(resources, str(operands[0]))
                    font_name = str(operands[0])
                    fs = _num(operands[1])
                elif o == "Tc":
                    flush(); tc = _num(operands[0])
                elif o == "Tw":
                    flush(); tw = _num(operands[0])
                elif o == "Tz":
                    flush(); th = _num(operands[0]) / 100.0
                elif o == "TL":
                    tl = _num(operands[0])
                elif o == "Ts":
                    flush(); rise = _num(operands[0])
                elif o == "Tm":
                    flush()
                    tm = tlm = tuple(_num(x) for x in operands)
                elif o in ("Td", "TD"):
                    tx, ty = _num(operands[0]), _num(operands[1])
                    if o == "TD":
                        tl = -ty
                        flush()
                    tlm = mul((1, 0, 0, 1, tx, ty), tlm)
                    tm = tlm
                    if cur and o == "Td":
                        td_ops.append(idx)
                elif o == "T*":
                    flush()
                    tlm = mul((1, 0, 0, 1, 0, -tl), tlm); tm = tlm
                elif o == "Tj":
                    show(_raw(operands[0]), idx)
                elif o == "TJ":
                    for item in operands[0]:
                        item = item.get_object() if hasattr(item, "get_object") else item
                        if isinstance(item, (int, float, NumberObject, FloatObject)):
                            tm = mul((1, 0, 0, 1, -float(item) / 1000.0 * fs * th, 0), tm)
                        else:
                            show(_raw(item), idx)
                    # A TJ is treated as one op; kerning numbers stay inside it (see replace()).
                elif o in ("'", '"'):
                    flush()
                    tlm = mul((1, 0, 0, 1, 0, -tl), tlm); tm = tlm
                    show(_raw(operands[-1]), idx)
                elif o == "Do" and depth < 6:
                    flush()
                    xo = (resources.get("/XObject") or {}).get(str(operands[0])) if resources is not None else None
                    if xo is not None:
                        xo = xo.get_object()
                        if str(xo.get("/Subtype")) == "/Form":
                            m = tuple(_num(x) for x in xo.get("/Matrix", [1, 0, 0, 1, 0, 0]))
                            r2 = xo.get("/Resources")
                            r2 = r2.get_object() if r2 is not None else resources
                            self._walk(page, xo, r2, mul(m, ctm), path + (f"@{idx}",), out, depth + 1, clip)
            except (IndexError, ValueError, TypeError, KeyError):
                flush()
                continue
        flush()

    def _make_run(self, page, path, glyphs, meta, glyph_ops, td_ops) -> Run:
        font: FontInfo = meta["font"]
        text, prev = [], None
        for g in glyphs:
            if prev is not None and (g.x0 - prev.x1) > SPACE_GAP_EM * g.em and not (prev.ch.isspace() or g.ch.isspace()):
                text.append(" ")
            text.append(g.ch or "�")
            prev = g
        em = meta["size"]
        x0, x1 = glyphs[0].x0, glyphs[-1].x1
        y = glyphs[0].y
        bbox = (min(x0, x1), y - 0.22 * em, max(x0, x1), y + 0.82 * em)
        i0, i1 = glyph_ops[0], glyph_ops[-1]
        rid = f"p{page + 1}:{'/'.join(path) or 'page'}:{i0}-{i1}"
        tds = [i for i in td_ops if i0 < i <= i1] if td_ops else []
        return Run(id=rid, page=page, container=path, text="".join(text), bbox=bbox, x0=x0, x1=x1, y=y, size=em, font_key=font.key,
                   font_name=font.base, i0=i0, i1=i1, glyphs=glyphs, tj_start=i0, td_indices=tds, tf=meta.get("tf", ("", 0.0)), xem=meta.get("xem", em), tc_user=meta.get("tc_user", 0.0),
                   tc_raw=meta.get("tc_raw", 0.0), th_raw=meta.get("th_raw", 1.0), rot=meta.get("rot", 0.0), clip=meta.get("clip"))

    def find(self, run_id: str) -> Run:
        m = re.match(r"p(\d+):", run_id)
        if not m:
            raise EditRefused("unknown_target", "That text could not be found.")
        for r in self.runs(int(m.group(1)) - 1):
            if r.id == run_id:
                return r
        raise EditRefused("unknown_target", "That text could not be found.")

    def run_status(self, run: Run) -> tuple[bool, str | None]:
        """Is this run structurally editable?  (Glyph availability and fit are checked per edit.)"""
        try:
            font = self._font_for(run)
        except EditRefused as err:
            return False, err.code
        if not font.supported:
            return False, "font_unsupported"
        if font.estimated_widths:
            return False, "font_metrics_unknown"
        if not self.is_visible(run):
            return False, "not_visible"
        if "\ufffd" in run.text:
            return False, "undecodable_text"
        cont = self._containers[(run.page, run.container)]
        if sum(cont.opcount.get(i, 0) for i in range(run.i0, run.i1 + 1)) != len(run.glyphs):
            return False, "shared_operator"
        return True, None

    def page_box(self, page: int) -> tuple[float, float, float, float]:
        pg = self.writer.pages[page]
        box = pg.cropbox if "/CropBox" in pg else pg.mediabox
        return float(box.left), float(box.bottom), float(box.right), float(box.top)

    def is_visible(self, run: Run) -> bool:
        """Text must intersect the page's visible box (tiled exports draw the same content on every page, clipped)."""
        l, b, r, t = self.page_box(run.page)
        x0, y0, x1, y1 = run.bbox
        return x1 > l + 0.5 and x0 < r - 0.5 and y1 > b + 0.5 and y0 < t - 0.5

    # -- fit analysis
    def bounds(self, run: Run) -> dict[str, Any]:
        """Horizontal room around a run: neighbouring text, table borders / rules, clipping and the page margin.

        `cell` is True only when a border was found on BOTH sides (a reliably detected table cell); otherwise the limits come from
        neighbouring text and the page margin alone (the pre-existing rule)."""
        left_box, _b, right_box, _t = self.page_box(run.page)
        left, right = min(left_box + 18.0, run.x0), max(right_box - 18.0, run.x1)
        for other in self.runs(run.page):
            if other.id == run.id:
                continue
            ox0, oy0, ox1, oy1 = other.bbox
            if oy1 > run.bbox[1] and oy0 < run.bbox[3]:
                if ox0 >= run.x1 - 0.5:
                    right = min(right, ox0 - NEIGHBOUR_GAP)
                elif ox1 <= run.x0 + 0.5:
                    left = max(left, ox1 + NEIGHBOUR_GAP)
        lo, hi = run.y - 0.1 * run.size, run.y + 0.7 * run.size
        lb = rb = None
        crossed = False
        for x, ylo, yhi in self._vlines.get(run.page, []):
            if yhi < lo or ylo > hi:
                continue
            if x >= run.x1 - 0.5:
                rb = x if rb is None else min(rb, x)
            elif x <= run.x0 + 0.5:
                lb = x if lb is None else max(lb, x)
            else:
                crossed = True
        if rb is not None:
            right = min(right, rb - 1.5)
        if lb is not None:
            left = max(left, lb + 1.5)
        clip_unknown = run.clip == "unknown"
        if isinstance(run.clip, tuple):
            right, left = min(right, run.clip[2] - 0.5), max(left, run.clip[0] + 0.5)
        return {"left": min(left, run.x0), "right": max(right, run.x1), "cell": lb is not None and rb is not None, "lb": lb, "rb": rb,
                "crossed": crossed, "clip_unknown": clip_unknown}

    def adjacent_right(self, run: Run) -> str:
        """Text of separate objects that touch the run's right edge on the same line (for example the `.00` after a whole-rupee amount)."""
        parts = sorted((o for o in self.runs(run.page) if o.id != run.id and abs(o.y - run.y) < 1.0 and -0.5 <= o.x0 - run.x1 <= 1.0), key=lambda o: o.x0)
        return "".join(o.text for o in parts).strip()

    def free_right(self, run: Run) -> float:
        """Space (pt) available to the right of the run before the next text object, table border or the page margin."""
        return max(self.bounds(run)["right"] - run.x1, 0.0)

    def plan_fit(self, run: Run, new_w: float, n_chars: int) -> dict[str, Any]:
        """Which strategies place `new_w` pt of text without touching neighbours, borders or the clip.  Pure geometry; changes nothing.

        Order of preference (least visually disruptive first): A natural placement (original size, alignment kept), B limited size
        reduction, C limited horizontal scaling, D limited letter-spacing, E use the free space of the same cell on both sides."""
        old_w = run.x1 - run.x0
        b = self.bounds(run)
        grows = new_w > old_w + 0.01
        if not grows:
            return {"natural": True, "shift": 0.0, "options": [], "old_w": old_w, "bounds": b, "anchor": "left"}
        if run.rot > 0.02 or run.rot != run.rot:
            return {"natural": False, "options": [], "old_w": old_w, "bounds": b, "anchor": "left", "blocked": ("rotated_text", "This text is rotated or transformed, so it can't be made longer safely.")}
        if b["crossed"]:
            return {"natural": False, "options": [], "old_w": old_w, "bounds": b, "anchor": "left", "blocked": ("border_inside_text", "A table line crosses this text, so it can't be made longer safely.")}
        if b["clip_unknown"]:
            return {"natural": False, "options": [], "old_w": old_w, "bounds": b, "anchor": "left", "blocked": ("clipped_text", "This text sits inside a clipping region that can't be measured, so it can't be made longer safely.")}
        anchor = "left"
        if b["cell"]:
            ml, mr = run.x0 - b["lb"], b["rb"] - run.x1
            if abs(ml - mr) < 1.5:
                anchor = "center"
            elif mr < ml and mr <= 0.35 * (b["rb"] - b["lb"]):
                anchor = "right"
        left, right = b["left"], b["right"]

        def avail_for() -> float:
            if anchor == "left":
                return right - run.x0
            if anchor == "right":
                return run.x1 - left
            half = min(run.x0 - left, right - run.x1)
            return old_w + 2 * max(half, 0.0)

        def shift_for(width: float) -> float:
            grow = width - old_w
            return 0.0 if anchor == "left" else (-grow if anchor == "right" else -grow / 2.0)

        avail = avail_for()
        if new_w <= avail + 0.01:
            return {"natural": True, "shift": shift_for(new_w), "options": [], "old_w": old_w, "bounds": b, "anchor": anchor, "avail": avail}
        n = max(n_chars, 1)
        glyph_w = max(new_w - n * run.tc_user, 1e-6)
        options: list[dict[str, Any]] = []
        fs = run.tf[1] or 1.0
        s_need = math.floor(((avail - n * run.tc_user) / glyph_w) * 200) / 200
        if FIT_MIN_FACTOR <= s_need < 1.0:
            w2 = glyph_w * s_need + n * run.tc_user
            options.append({"strategy": "size", "factor": s_need, "new_width": w2, "shift": shift_for(w2), "size_after": round(run.size * s_need, 2),
                            "label": f"Reduce the text size by {round((1 - s_need) * 100)}%", "detail": f"{run.size:.1f} pt → {run.size * s_need:.1f} pt"})
        c_need = math.floor((avail / new_w) * 200) / 200
        if FIT_MIN_FACTOR <= c_need < 1.0:
            w2 = new_w * c_need
            options.append({"strategy": "scale", "factor": c_need, "new_width": w2, "shift": shift_for(w2),
                            "label": f"Condense the text horizontally to {round(c_need * 100)}%", "detail": f"Characters keep their height; width {round(c_need * 100)}% of normal"})
        d = (new_w - avail) / max(n - 1, 1)
        if 0 < d <= FIT_MAX_SPACING_EM * run.size and run.xem > 0:
            w2 = new_w - (n - 1) * d
            options.append({"strategy": "spacing", "factor": d, "new_width": w2, "shift": shift_for(w2),
                            "label": f"Tighten the letter spacing by {d / run.size:.2f} em", "detail": "Same size and shape; characters sit slightly closer"})
        full = right - left
        if b["cell"] and new_w <= full + 0.01 and anchor != "center":
            cx = (run.x0 + run.x1) / 2.0
            start = min(max(cx - new_w / 2.0, left), right - new_w)
            options.append({"strategy": "expand", "factor": start - run.x0, "new_width": new_w, "shift": start - run.x0,
                            "label": "Use the free space of the same cell", "detail": "Same size; the text is centred on its old position within the cell"})
        order = {"size": 0, "scale": 1, "spacing": 2, "expand": 3}
        options.sort(key=lambda o: order[o["strategy"]])
        return {"natural": False, "options": options, "old_w": old_w, "bounds": b, "anchor": anchor, "avail": avail}

    def measure(self, run: Run, text: str) -> tuple[float, bytes | None, list[str]]:
        font = self._font_for(run)
        missing: list[str] = []
        width = 0.0
        tc = 0.0
        for ch in text:
            code = font.code_for(ch)
            if code is None or font.has_glyph(ch) is False:
                if ch == " " and code is None:
                    width += 0.25 * run.xem + run.tc_user
                    continue
                missing.append(ch)
                continue
            width += font.width(code) / 1000.0 * run.xem + run.tc_user
        return width + tc, None, missing

    def _font_for(self, run: Run) -> FontInfo:
        cont = self._containers[(run.page, run.container)]
        fi = self._font(cont.resources, run.font_key)
        if fi is None:
            raise EditRefused("font_unavailable", "The font for this text could not be read.")
        return fi

    # -- the edit
    def _prepare(self, run_id: str, new_text: str, expect_old: str | None):
        """Common checks for an edit and for a fit report: (run, font, width of the new text, substitute font or None)."""
        run = self.find(run_id)
        if expect_old is not None and expect_old != run.text:
            raise EditRefused("stale_target", "This text changed since it was selected.")
        if new_text == run.text:
            raise EditRefused("no_change", "There is no change to save.")
        if "\n" in new_text or "\r" in new_text:
            raise EditRefused("multiline_not_supported", "Edit one line at a time.")
        font = self._font_for(run)
        if not font.supported:
            raise EditRefused("font_unsupported", "This text uses a font encoding that can't be edited safely.")
        if font.estimated_widths:
            raise EditRefused("font_metrics_unknown", "This text uses a font without width data, so a safe fit check isn't possible.")
        new_w, _, missing = self.measure(run, new_text)
        substitute: str | None = None
        if missing:
            substitute = _substitute_name(run.font_name)
            unencodable = [c for c in dict.fromkeys(new_text) if not _winansi(c)]
            if unencodable:
                raise EditRefused("glyph_unavailable",
                                  f"The original font doesn't include {', '.join(repr(c) for c in unencodable)} and no safe substitute "
                                  "has it. Use other characters or keep the original wording.")
            new_w = sum(_std_width(substitute, c) / 1000.0 * run.xem + run.tc_user for c in new_text)
        return run, font, new_w, substitute

    def fit_report(self, run_id: str, new_text: str, *, expect_old: str | None = None) -> dict[str, Any]:
        """What would happen if `new_text` replaced the run: fits as-is, which fitting options exist, or why not.  Changes nothing."""
        run, _font, new_w, _sub = self._prepare(run_id, new_text, expect_old)
        plan = self.plan_fit(run, new_w, len(new_text))
        out: dict[str, Any] = {"id": run_id, "old_width": round(plan["old_w"], 2), "new_width": round(new_w, 2), "fits": bool(plan["natural"]),
                               "options": [{**o, "new_width": round(o["new_width"], 2), "shift": round(o["shift"], 2), "factor": round(o["factor"], 4)} for o in plan["options"]],
                               "refusal": None, "cell_detected": bool(plan["bounds"]["cell"]), "anchor": plan["anchor"]}
        if "blocked" in plan:
            out["refusal"] = {"code": plan["blocked"][0], "message": plan["blocked"][1]}
        elif not plan["natural"] and not plan["options"]:
            out["refusal"] = {"code": "overflow", "message": f"The new text is {new_w - plan['old_w']:.1f} pt wider than the original and no safe fitting option "
                                                            "(a small size, width or spacing change, or the free space in its cell) makes it fit. Shorten it."}
        return out

    def replace(self, run_id: str, new_text: str, *, expect_old: str | None = None, allow_grow: bool = False, fit: str | None = None) -> dict[str, Any]:
        run, font, new_w, substitute = self._prepare(run_id, new_text, expect_old)
        old_w = run.x1 - run.x0
        plan = self.plan_fit(run, new_w, len(new_text))
        choice: dict[str, Any] | None = None
        shift = 0.0
        if allow_grow:
            pass
        elif plan["natural"]:
            shift = plan.get("shift", 0.0)
        else:
            if "blocked" in plan:
                raise EditRefused(plan["blocked"][0], plan["blocked"][1], {"options": []})
            avail = plan["avail"]
            if fit is not None:
                if fit not in FIT_STRATEGIES:
                    raise EditRefused("fit_unknown", "That fitting option isn't available.")
                choice = next((o for o in plan["options"] if o["strategy"] == fit), None)
                if choice is None:
                    raise EditRefused("fit_unavailable", "That fitting option no longer fits this text. Choose another option or shorten the text.",
                                      {"options": [{k: v for k, v in o.items()} for o in plan["options"]]})
                shift, new_w = choice["shift"], choice["new_width"]
            else:
                raise EditRefused("overflow", f"The new text is {new_w - old_w:.1f} pt wider than the original and would run into "
                                              f"neighbouring content or the cell border (room for {avail - old_w:.1f} pt). "
                                              + ("Choose a fitting option or shorten it." if plan["options"] else "Shorten it."),
                                  {"options": [{k: v for k, v in o.items()} for o in plan["options"]]})
        self._privatise(run)
        run = self.find(run_id)
        cont = self._containers[(run.page, run.container)]
        ops = cont.ops
        if sum(cont.opcount.get(i, 0) for i in range(run.i0, run.i1 + 1)) != len(run.glyphs):
            raise EditRefused("shared_operator", "This text shares a drawing instruction with neighbouring text and can't be changed on its own.")
        # A marked-content span with /ActualText overrides the text extractors read: remove it where it encloses the
        # edited glyphs, otherwise search / copy / accessibility would still return the OLD wording.
        for bdc in sorted({b for i in range(run.i0, run.i1 + 1) for b in cont.actual.get(i, ())}):
            props = ops[bdc][0][1] if len(ops[bdc][0]) > 1 else None
            if isinstance(props, DictionaryObject):
                if "/ActualText" in props:
                    del props["/ActualText"]
            else:
                raise EditRefused("marked_content_unsupported", "This text is wrapped in tagged content that can't be updated safely.")
        # Fold removed relative moves into one Td so everything after the run keeps its absolute position.
        sx = sy = 0.0
        for i in run.td_indices:
            sx += float(ops[i][0][0]); sy += float(ops[i][0][1])
        pieces: list[bytes | float] = []
        buf = b""
        lead = len(new_text) - len(new_text.lstrip(" "))
        for pos_i, ch in enumerate([] if substitute else new_text):
            code = font.code_for(ch)
            if pos_i < lead:           # leading spaces become an advance (a leading space glyph confuses some extractors)
                pieces.append(font.width(code) if code is not None else 0.25 * 1000.0)
                continue
            if code is None:           # a space the font has no glyph for: advance instead of drawing
                if buf:
                    pieces.append(buf); buf = b""
                pieces.append(0.25 * 1000.0)
                continue
            buf += code.to_bytes(font.code_len, "big")
        if buf:
            pieces.append(buf)
        if substitute:
            pieces = [new_text.encode("cp1252")]
        if shift:
            # Move the pen with a TJ adjustment (not Td): it works whatever the text matrix was, and ends where the text now ends.
            hf = choice["factor"] if choice and choice["strategy"] in ("size", "scale") else 1.0
            pieces.insert(0, shift / (run.xem * hf) * 1000.0)
        arr = ArrayObject()
        for p in pieces:
            arr.append(ByteStringObject(p) if isinstance(p, bytes) else FloatObject(-p))
        show_op = ([arr], b"TJ") if any(not isinstance(p, bytes) for p in pieces) else ([ByteStringObject(pieces[0] if pieces else b"")], b"Tj")
        # operator indices of the run: i0..i1 inclusive; keep non-show/non-Td operators that lie between them.
        keep_between = [ops[i] for i in range(run.i0, run.i1 + 1)
                        if (i not in run.td_indices) and _opname(ops[i][1]) not in ("Tj", "TJ")]
        new_block = [show_op] + keep_between
        strat = choice["strategy"] if choice else None
        size_factor = choice["factor"] if strat == "size" else 1.0
        pre: list = []
        post: list = []
        if substitute or strat == "size":
            font_key = self._ensure_font(cont, substitute) if substitute else run.tf[0]
            pre.append(([NameObject(font_key), FloatObject(run.tf[1] * size_factor)], b"Tf"))
            post.append(([NameObject(run.tf[0]), FloatObject(run.tf[1])], b"Tf"))
        if strat == "scale":
            pre.append(([FloatObject(run.th_raw * 100.0 * choice["factor"])], b"Tz"))
            post.append(([FloatObject(run.th_raw * 100.0)], b"Tz"))
        if strat == "spacing":
            tc_new = run.tc_raw - choice["factor"] * (run.tf[1] / run.xem)
            pre.append(([FloatObject(tc_new)], b"Tc"))
            post.append(([FloatObject(run.tc_raw)], b"Tc"))
        new_block = pre + new_block + post
        if sx or sy:
            new_block.append(([FloatObject(sx), FloatObject(sy)], b"Td"))
        ops[run.i0:run.i1 + 1] = new_block
        self._invalidate(run.page)
        self._write_back(cont)
        cells, pos = [], 0.0
        for ch in new_text:
            if substitute:
                gw = _std_width(substitute, ch) / 1000.0 * run.xem
            else:
                code = font.code_for(ch)
                gw = (font.width(code) / 1000.0 * run.xem) if code is not None else 0.25 * run.xem
            if strat == "size":
                w = gw * size_factor + run.tc_user
            elif strat == "scale":
                w = (gw + run.tc_user) * choice["factor"]
            elif strat == "spacing":
                w = gw + run.tc_user - choice["factor"]
            else:
                w = gw + run.tc_user
            cells.append((pos, pos + w)); pos += w
        new_x0 = run.x0 + shift
        return {"id": run_id, "old": run.text, "new": new_text, "old_width": old_w, "new_width": new_w,
                "room": plan.get("avail", old_w + self.free_right(run)), "font": run.font_name, "size": run.size, "substitute_font": substitute, "cells": cells,
                "strategy": strat, "size_factor": size_factor, "new_x0": new_x0, "extent": [min(run.x0, new_x0), max(run.x1, new_x0 + new_w)]}

    # -- copy-on-write so an edit never leaks into other pages / other uses of a shared form
    def _own(self, obj) -> int:
        self._owned.add(id(obj))
        return id(obj)

    def _own_resources(self, cont: _Container) -> DictionaryObject:
        res = cont.resources
        if id(res) in self._owned:
            return res
        new = DictionaryObject(dict(res))
        for key in ("/XObject", "/Font"):
            sub = res.get(key)
            if sub is not None:
                copy = DictionaryObject(dict(sub.get_object()))
                self._own(copy)
                new[NameObject(key)] = copy
        self._own(new)
        cont.obj[NameObject("/Resources")] = new
        cont.resources = new
        return new

    def _privatise(self, run: Run) -> None:
        page = run.page
        self.runs(page)
        for k in range(len(run.container)):
            pc = self._containers[(page, run.container[:k])]
            idx = int(run.container[k][1:])
            name = str(pc.ops[idx][0][0])
            if name.startswith("/C360X"):
                continue
            res = self._own_resources(pc)
            xo = res["/XObject"].get_object()
            form = xo[name].get_object()
            clone = DecodedStreamObject()
            clone.set_data(form.get_data())
            for key, value in form.items():
                if key not in ("/Length", "/Filter", "/DecodeParms", "/DL", "/Resources"):
                    clone[NameObject(key)] = value
            if "/Resources" in form:
                r = form["/Resources"].get_object()
                rc = DictionaryObject(dict(r))
                for key in ("/XObject", "/Font"):
                    if key in r:
                        sub = DictionaryObject(dict(r[key].get_object()))
                        self._own(sub)
                        rc[NameObject(key)] = sub
                self._own(rc)
                clone[NameObject("/Resources")] = rc
            self._counter += 1
            new_name = f"/C360X{self._counter}"
            xo[NameObject(new_name)] = self.writer._add_object(clone)
            pc.ops[idx] = ([NameObject(new_name)], b"Do")
            self._write_back(pc)
            self._invalidate(page)
            self.runs(page)

    def _ensure_font(self, cont: _Container, std_name: str) -> str:
        """Register a non-embedded standard-14 font (WinAnsi) in the container's own resources and return its key."""
        key = "/C360" + re.sub(r"[^A-Za-z0-9]", "", std_name)
        res = self._own_resources(cont)
        fonts = res.get("/Font")
        if fonts is None:
            fonts = DictionaryObject()
            res[NameObject("/Font")] = fonts
        fonts = fonts.get_object()
        if key not in fonts:
            fonts[NameObject(key)] = self.writer._add_object(DictionaryObject({
                NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/" + std_name), NameObject("/Encoding"): NameObject("/WinAnsiEncoding")}))
        return key

    def _invalidate(self, page):
        self._runs.pop(page, None)
        for k in [k for k in self._containers if k[0] == page]:
            del self._containers[k]

    def _write_back(self, cont: _Container):
        data = cont.cs.get_data()
        if hasattr(cont.obj, "get_contents") and "/Contents" in cont.obj:
            stream = DecodedStreamObject()
            stream.set_data(data)
            cont.obj[NameObject("/Contents")] = self.writer._add_object(stream.flate_encode())
        else:
            cont.obj.set_data(data)
            try:
                cont.obj.flate_encode()
            except Exception:  # already encoded streams keep their own filter
                pass

    def save(self) -> bytes:
        # NB: pypdf's compress_identical_objects(remove_orphans=...) drops images in some files; never use it here.
        buf = io.BytesIO()
        self.writer.write(buf)
        return buf.getvalue()


def _merge_clip(current: Any, new: Any) -> Any:
    if current == "unknown" or new == "unknown":
        return "unknown"
    if current is None:
        return new
    return (max(current[0], new[0]), max(current[1], new[1]), min(current[2], new[2]), min(current[3], new[3]))


def _winansi(ch: str) -> bool:
    try:
        ch.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def _substitute_name(base: str) -> str:
    """Closest standard-14 family/style for an original font name (only used when glyphs are missing)."""
    n = base.split("+")[-1].lower()
    bold = any(k in n for k in ("bold", "black", "heavy", "semibold", "demi"))
    italic = any(k in n for k in ("italic", "oblique"))
    if any(k in n for k in ("courier", "mono", "consolas", "typewriter")):
        fam = ("Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique")
    elif any(k in n for k in ("times", "georgia", "garamond", "palatino", "cambria", "serif", "book", "minion")) and "sans" not in n:
        fam = ("Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic")
    else:
        fam = ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique")
    return fam[(1 if bold else 0) + (2 if italic else 0)]


def _page_resources(pg) -> DictionaryObject:
    node = pg
    for _ in range(32):                      # /Resources may be inherited from the page tree
        res = node.get("/Resources")
        if res is not None:
            return res.get_object()
        parent = node.get("/Parent")
        if parent is None:
            break
        node = parent.get_object()
    return DictionaryObject()


def _opname(op) -> str:
    return op.decode() if isinstance(op, bytes) else str(op)


def _raw(s) -> bytes:
    s = s.get_object() if hasattr(s, "get_object") else s
    if isinstance(s, ByteStringObject) or isinstance(s, (bytes, bytearray)):
        return bytes(s)
    ob = getattr(s, "original_bytes", None)
    return ob if ob is not None else str(s).encode("latin-1", "replace")


def edit_order(run_ids: list[str]) -> list[str]:
    """Order ids so earlier edits never shift operator indices that later edits depend on:
    nested form XObjects first, then higher operator index before lower within a content stream."""
    def key(rid: str):
        m = re.match(r"p(\d+):(.*):(\d+)-(\d+)$", rid)
        page, path, i0 = int(m.group(1)), m.group(2), int(m.group(3))
        depth = 0 if path == "page" else path.count("/") + 1
        return (page, -depth, path, -i0)
    return sorted(run_ids, key=key)
