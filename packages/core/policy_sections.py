"""Rule-based, source-linked sections and facts for insurance policy documents.

Deterministic (no AI, no model calls). The input is the document's visible text as positioned lines
({page, lines:[{t,x,y,s,b}]}); the output is a list of sections, each with the pages and first-line position it
came from, plus label/value facts with the same anchors. Nothing is generated: every string shown is lifted from
the document, and a section is only reported when a heading in the document matches that section's vocabulary.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any

# category -> (title shown to the user, heading patterns)
CATEGORIES: list[tuple[str, str, list[str]]] = [
    ("schedule", "Policy schedule", [r"\bpolicy\s+schedule\b", r"\bschedule\s+of\s+(?:insurance|cover|policy)", r"\bcertificate\s+of\s+insurance\b",
                                     r"^schedule$", r"\bpolicy\s+(?:details|particulars|summary)\b", r"\bpolicy\s+certificate\b"]),
    ("insured", "Insured details", [r"\binsured(?:'s)?\s+(?:details|name|particulars|information)", r"\bdetails\s+of\s+(?:the\s+)?(?:insured|proposer|policy\s*holder)",
                                    r"\b(?:policy\s*holder|proposer|member|customer)\s+(?:details|information)", r"^insured$", r"\binsured\s*/\s*proposer"]),
    ("coverage", "Coverage", [r"\bcoverage\b", r"\bscope\s+of\s+cover", r"\bwhat\s+is\s+covered", r"\binsuring\s+(?:clause|agreement)", r"\bsection\s+[ivx\d]+\s*[-–:]?\s*(?:cover|loss|damage|material|liability)",
                              r"\bperils?\s+covered", r"\bbenefits?\s+(?:covered|under|summary)", r"\bcover\s+(?:details|provided)", r"^cover$", r"\bsection\s+[ivx]+\b"]),
    ("sum_insured", "Sums insured", [r"\bsums?\s+insured\b", r"\bsum\s+assured\b", r"\blimits?\s+of\s+(?:liability|indemnity)", r"\bbasis\s+of\s+valuation", r"\binsured\s+value"]),
    ("premium", "Premium and taxes", [r"\bpremium\b", r"\b(?:gst|igst|cgst|sgst|service\s+tax)\b", r"\btaxes\b", r"\bstamp\s+duty\b", r"\bamount\s+payable\b"]),
    ("addons", "Add-ons and extensions", [r"\badd[\s-]*ons?\b", r"\boptional\s+(?:cover|benefit)", r"\bextensions?\b", r"\bendorsements?\b", r"\bnomination\b"]),
    ("warranties", "Warranties", [r"\bwarrant(?:y|ies)\b"]),
    ("exclusions", "Exclusions", [r"\bexclusions?\b", r"\bwhat\s+is\s+not\s+covered", r"\bnot\s+covered\b", r"\bgeneral\s+exceptions?\b", r"\bpermanent\s+exclusions"]),
    ("conditions", "Conditions", [r"\b(?:general|special|policy|standard)?\s*conditions?\b", r"\bterms\s+(?:and|&)\s+conditions\b", r"\bterms\s+of\s+(?:the\s+)?policy"]),
    ("clauses", "Clauses", [r"\bclauses?\b"]),
]
COMPILED = [(key, title, [re.compile(p, re.I) for p in pats]) for key, title, pats in CATEGORIES]
TITLES = {key: title for key, title, _ in CATEGORIES}

# label -> fact key, matched against the label text of "Label : value" lines and two-cell rows
FACT_LABELS: list[tuple[str, str, list[str]]] = [
    ("policy_number", "Policy number", [r"^policy\s*(?:no\.?|number|#)$", r"^certificate\s*(?:no\.?|number)$"]),
    ("period", "Period of insurance", [r"^(?:policy\s+)?period(?:\s+of\s+(?:insurance|cover))?$", r"^(?:from|to)\s*(?:date)?$", r"^(?:start|expiry|end)\s+date$", r"^(?:risk\s+)?(?:inception|commencement)\s+date$"]),
    ("insured_name", "Insured", [r"^(?:name\s+of\s+(?:the\s+)?)?(?:insured|proposer|policy\s*holder)(?:'s)?(?:\s+name)?$", r"^customer\s+name$"]),
    ("address", "Address", [r"^(?:insured'?s?\s+|communication\s+|correspondence\s+)?address$"]),
    ("sum_insured", "Sum insured", [r"^(?:total\s+)?sum\s+insured$", r"^sum\s+assured$", r"^total\s+(?:insured\s+)?value$"]),
    ("premium", "Premium", [r"^(?:net\s+|basic\s+|total\s+)?premium(?:\s+payable)?(?:\s*\(.*\))?$"]),
    ("tax", "Tax", [r"^(?:gst|igst|cgst|sgst|service\s+tax)(?:\s*@?\s*[\d.]+\s*%?)?$", r"^taxes?$"]),
    ("deductible", "Deductible / excess", [r"^(?:deductible|excess|co-?pay)(?:s)?$"]),
    ("insurer", "Insurer", [r"^(?:insurer|insurance\s+company|company\s+name)$"]),
    ("intermediary", "Intermediary", [r"^(?:agent|broker|intermediary)(?:\s+name)?$"]),
]
COMPILED_FACTS = [(key, title, [re.compile(p, re.I) for p in pats]) for key, title, pats in FACT_LABELS]


@dataclass
class Line:
    page: int
    text: str
    x: float
    y: float
    size: float
    bold: bool
    segs: list[str] = field(default_factory=list)


@dataclass
class Section:
    key: str
    title: str
    heading: str
    start: Line
    lines: list[Line] = field(default_factory=list)

    def pages(self) -> list[int]:
        return sorted({self.start.page, *(l.page for l in self.lines)})


def _flatten(outline: dict[str, Any]) -> list[Line]:
    out = []
    for page in outline.get("pages", []):
        for ln in page.get("lines", []):
            # An unmapped glyph between two letters is almost always an apostrophe; anywhere else it is dropped, never shown.
            clean = lambda v: re.sub(r"\s{2,}", " ", re.sub(r"(?<=[A-Za-z])\ufffd(?=[A-Za-z])", "'", str(v)).replace("\ufffd", "")).strip()
            segs = [clean(x["t"]) for x in ln.get("segs", []) if clean(x.get("t", ""))] or [clean(ln["t"])]
            out.append(Line(int(page["page"]), clean(ln["t"]), float(ln["x"]), float(ln["y"]), float(ln["s"]), bool(ln["b"]), segs))
    return [l for l in out if l.text]


def _body_size(lines: list[Line]) -> float:
    """The font size carrying most of the text (by characters), robust for short documents."""
    weight: dict[float, int] = {}
    for l in lines:
        weight[l.size] = weight.get(l.size, 0) + len(l.text)
    return max(weight, key=lambda k: weight[k])


def _is_heading_shape(line: Line, body: float) -> bool:
    t = line.text
    if len(t) > 80 or len(t) < 3:
        return False
    if re.search(r"[.;,]$", t) and not t.isupper():
        return False
    if re.search(r"[:：]\s*\S", t) or sum(c.isdigit() for c in t) > 0.25 * len(t):
        return False                     # a labelled value or a figure row is data, not a heading
    letters = [c for c in t if c.isalpha()]
    if not letters:
        return False
    upper = sum(c.isupper() for c in letters) / len(letters)
    return line.bold or line.size >= body * 1.15 or (upper > 0.85 and len(t) <= 60)


def classify_heading(text: str) -> str | None:
    clean = re.sub(r"[\s:–-]+$", "", text.strip())
    clean = re.sub(r"^[\dIVXivx]+[.)]\s*", "", clean)
    for key, _title, pats in COMPILED:
        if any(p.search(clean) for p in pats):
            return key
    return None


FACT_SECTION = {"policy_number": "schedule", "period": "schedule", "insurer": "schedule", "intermediary": "schedule",
                "insured_name": "insured", "address": "insured", "sum_insured": "sum_insured",
                "premium": "premium", "tax": "premium", "deductible": "conditions"}


def _excerpt(lines: list[Line], limit: int = 170) -> str:
    out = ""
    for ln in lines:
        if len(ln.text) < 3:
            continue
        out = (out + " " + ln.text).strip()
        if len(out) >= limit:
            break
    if len(out) <= limit:
        return out
    cut = out[:limit]
    return (cut[:cut.rfind(" ")] if " " in cut else cut).rstrip(" ,;:-") + "…"      # never cut mid-word


def segment(outline: dict[str, Any]) -> dict[str, Any]:
    """One logical section per supported category, each with every occurrence anchored to page and position."""
    lines = _flatten(outline)
    if not lines:
        return {"sections": [], "confidence": "none", "pages": 0}
    body = _body_size(lines)
    raw: list[Section] = []
    current: Section | None = None
    for ln in lines:
        if _is_heading_shape(ln, body):
            key = classify_heading(ln.text)
            if key:
                current = Section(key, TITLES[key], ln.text, ln)
                raw.append(current)
                continue
            if current is not None and (ln.bold or ln.size >= body * 1.15) and len(ln.text) <= 60 and ln.text.isupper():
                current = None           # a different, unrecognised major heading ends the section
                continue
        if current is not None:
            current.lines.append(ln)
    pages = max(l.page for l in lines)
    facts = _facts(lines)
    grouped: dict[str, dict[str, Any]] = {}
    for s in raw:
        g = grouped.setdefault(s.key, {"key": s.key, "title": s.title, "occurrences": [], "facts": [], "words": 0})
        same_page = [l for l in s.lines if l.page == s.start.page]
        ex_lines = same_page or [l for l in s.lines if s.lines and l.page == s.lines[0].page]
        g["occurrences"].append({"page": s.start.page, "y": s.start.y, "heading": s.heading, "excerpt": _excerpt(ex_lines),
                                 "excerpt_page": ex_lines[0].page if ex_lines else s.start.page, "lines": len(s.lines)})
        g["words"] += sum(len(l.text.split()) for l in s.lines)
    for f in facts:
        key = FACT_SECTION.get(f["key"])
        if key:
            g = grouped.setdefault(key, {"key": key, "title": TITLES[key], "occurrences": [], "facts": [], "words": 0})
            g["facts"].append(f)
    sections = sorted(grouped.values(), key=lambda g: min([o["page"] * 10000 + o["y"] for o in g["occurrences"]] + [f["page"] * 10000 + f["y"] for f in g["facts"]]))
    for g in sections:
        pg = sorted({o["page"] for o in g["occurrences"]} | {f["page"] for f in g["facts"]})
        g["pages"] = pg
    covered = {p for g in sections for p in g["pages"]}
    confidence = "high" if len(sections) >= 3 and len(covered) >= max(1, min(pages, 3)) else "low" if sections else "none"
    return {"sections": sections, "confidence": confidence, "pages": pages, "body_size": body}


def _facts(lines: list[Line]) -> list[dict[str, Any]]:
    """'Label : value' inside one cell, or a label cell ending in ':' followed by a value cell on the same line."""
    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(ln: Line, label: str, value: str) -> bool:
        label, value = label.strip(), value.strip()
        if not value or len(value) > 160:
            return False
        for key, title, pats in COMPILED_FACTS:
            if any(p.match(label) for p in pats):
                if (key, value) not in seen:
                    seen.add((key, value))
                    facts.append({"key": key, "label": title, "as_written": label, "value": value, "page": ln.page, "y": ln.y, "x": ln.x})
                return True
        return False

    pair = re.compile(r"^(?P<label>[A-Za-z][A-Za-z .'()/&-]{2,50}?)\s*[:：]\s*(?P<value>\S.*)$")
    label_only = re.compile(r"^(?P<label>[A-Za-z][A-Za-z .'()/&-]{2,50}?)\s*[:：]\s*$")
    for ln in lines:
        for i, seg in enumerate(ln.segs):
            m = pair.match(seg)
            if m:
                add(ln, m.group("label"), m.group("value"))
                continue
            lo = label_only.match(seg)
            if lo and i + 1 < len(ln.segs):
                add(ln, lo.group("label"), ln.segs[i + 1])
    return facts


def page_label(pages: list[int]) -> str:
    """[1, 3, 4, 5, 11] -> 'pages 1, 3–5, 11'"""
    if not pages:
        return ""
    runs, start, prev = [], pages[0], pages[0]
    for p in pages[1:] + [None]:  # type: ignore[list-item]
        if p is not None and p == prev + 1:
            prev = p
            continue
        runs.append(str(start) if start == prev else f"{start}–{prev}")
        if p is not None:
            start = prev = p
    return ("page " if len(pages) == 1 else "pages ") + ", ".join(runs)


def _quote(text: str, limit: int = 60) -> str:
    return "“" + (text if len(text) <= limit else text[:limit].rstrip() + "…") + "”"


def to_chunks(result: dict[str, Any]) -> list[dict[str, Any]]:
    """API/UI chunk payloads from a segmentation. Strings are lifted from the document; nothing is generated."""
    chunks = []
    for g in result["sections"]:
        occ = sorted(g["occurrences"], key=lambda o: (o["page"], o["y"]))
        headings = list(dict.fromkeys(o["heading"] for o in occ))
        where = page_label(g["pages"])
        parts = []
        if headings:
            parts.append("Headings found: " + ", ".join(_quote(h) for h in headings[:3]) + (f" and {len(headings) - 3} more" if len(headings) > 3 else ""))
        if g["facts"]:
            parts.append(f"{len(g['facts'])} labelled value{'s' if len(g['facts']) != 1 else ''} read from the page")
        summary = f"{where[0].upper()}{where[1:]}. " + ". ".join(parts) + "."
        points = [f"{f['label']}: {f['value']} (p. {f['page']})" for f in g["facts"][:3]]
        for o in occ:
            if len(points) >= 3:
                break
            if o["excerpt"]:
                points.append(f"{_quote(o['heading'], 40)} (p. {o.get('excerpt_page', o['page'])}): {o['excerpt']}")
        anchors = [{"page": o["page"], "y": o["y"], "label": o["heading"]} for o in occ[:40]] + \
                  [{"page": f["page"], "y": f["y"], "label": f["as_written"]} for f in g["facts"][:20]]
        anchors.sort(key=lambda a: (a["page"], a["y"]))
        chunks.append({
            "id": f"sec-{g['key']}", "title": g["title"], "summary": summary, "words": g["words"], "points": points,
            "method": "rules: headings and labels" + ("" if result["confidence"] == "high" else " (limited structure)"),
            "source": {"kind": "pdf", "label": where, "pages": g["pages"], "anchors": anchors},
            "facts": [{"label": f["label"], "value": f["value"], "page": f["page"], "y": f["y"]} for f in g["facts"]],
        })
    return chunks


def outline_chunks(outline: dict[str, Any]) -> list[dict[str, Any]]:
    """Fallback when no policy vocabulary matches: the document's own major headings, verbatim, with pages."""
    lines = _flatten(outline)
    if not lines:
        return []
    body = _body_size(lines)
    heads: list[tuple[Line, list[Line]]] = []
    for ln in lines:
        if _is_heading_shape(ln, body) and (ln.size >= body * 1.25 or (ln.bold and ln.text.isupper())):
            heads.append((ln, []))
        elif heads:
            heads[-1][1].append(ln)
    out, seen = [], set()
    for head, body_lines in heads:
        if head.text.lower() in seen:
            continue
        seen.add(head.text.lower())
        pages = sorted({head.page, *(l.page for l in body_lines)})
        out.append({"id": f"head-{len(out) + 1}", "title": head.text, "summary": f"{page_label(pages)[0].upper()}{page_label(pages)[1:]}.",
                    "words": sum(len(l.text.split()) for l in body_lines), "points": ([_excerpt(body_lines)] if body_lines else []),
                    "method": "rules: the document's own headings",
                    "source": {"kind": "pdf", "label": page_label(pages), "pages": pages, "anchors": [{"page": head.page, "y": head.y, "label": head.text}]},
                    "facts": []})
        if len(out) >= 14:
            break
    return out
