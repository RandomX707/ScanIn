"""Readable-text extraction used only to build the document map (chunks).

Pure string/structure transforms; untrusted container parsing (PDF, DOCX, XLSX, PPTX) happens in the
restricted parser worker, which returns the structured details these helpers consume.
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from html.parser import HTMLParser
from typing import Any

MAX_TEXT_BYTES = 5 * 1024 * 1024
TEXT_EXTENSIONS = frozenset({".txt", ".text", ".md", ".markdown", ".html", ".htm", ".xml"})


def decode_text(data: bytes) -> tuple[str, str]:
    """Decode a text document; returns ``(text, encoding)`` where the encoding round-trips the bytes."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8"), "utf-8-sig"
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), "latin-1"


_BLOCKS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "br", "blockquote", "pre"}
_SKIP = {"script", "style", "iframe", "object", "embed", "form", "svg", "math", "meta", "link", "video", "audio", "template", "head"}


class _PlainHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def _newline(self) -> None:
        joined = "".join(self.out)
        if joined and not joined.endswith("\n"):
            self.out.append("\n")

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _SKIP:
            self.skip += 1
            return
        if tag in _BLOCKS:
            self._newline()
        if re.fullmatch(r"h[1-6]", tag):
            self.out.append("## ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if tag in _BLOCKS:
            self._newline()

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)


def html_to_text(raw: str) -> str:
    parser = _PlainHtml()
    parser.feed(raw)
    parser.close()
    return re.sub(r"\n{3,}", "\n\n", "".join(parser.out)).strip()


def csv_text(name: str, raw: str) -> str:
    rows = list(csv.reader(io.StringIO(raw)))
    return f"# Table: {name}\n" + "\n".join(" | ".join(row) for row in rows)


def json_text(raw: str) -> str:
    try:
        return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except (ValueError, RecursionError):
        return raw


def docx_text(details: dict[str, Any]) -> str:
    lines: list[str] = []
    row_cells: list[str] = []
    for block in details.get("blocks", []) or []:
        text = str(block.get("text", "")).strip()
        if not text:
            continue
        if block.get("kind") == "table_cell":
            row_cells.append(text)
            continue
        if row_cells:
            lines.append(" | ".join(row_cells)); row_cells = []
        lines.append(f"## {text}" if block.get("heading_level") else text)
    if row_cells:
        lines.append(" | ".join(row_cells))
    return "\n".join(lines)


def _column_number(cell: str) -> tuple[int, int]:
    """(row, column) of an A1 reference or of the CSV adapter's ``row,column`` form."""
    pair = re.fullmatch(r"(\d+),(\d+)", cell)
    if pair:
        return int(pair.group(1)), int(pair.group(2))
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", cell)
    if not match:
        return 0, 0
    column = 0
    for char in match.group(1).upper():
        column = column * 26 + ord(char) - 64
    return int(match.group(2)), column


def xlsx_text(targets: list[dict[str, Any]], sheets: list[str]) -> str:
    grids: dict[str, dict[tuple[int, int], str]] = {name: {} for name in sheets}
    for target in targets:
        value = (target.get("value") or {}).get("value")
        if value is None:
            continue
        grids.setdefault(str(target["sheet"]), {})[_column_number(str(target["cell"]))] = str(value)
    parts = []
    for name, grid in grids.items():
        if not grid:
            continue
        rows = sorted({row for row, _ in grid})
        width = max(column for _, column in grid)
        lines = [" | ".join(grid.get((row, column), "") for column in range(1, width + 1)) for row in rows]
        parts.append(f"## Sheet: {name}\n" + "\n".join(lines))
    return "\n\n".join(parts)


def pdf_text(details: dict[str, Any]) -> str:
    pages = []
    for page in details.get("pages", []) or []:
        lines = [line.strip() for line in str(page.get("text", "")).replace("\r", "\n").split("\n") if line.strip()]
        pages.append(f"## Page {page['page']}\n" + "\n".join(lines))
    text = "\n\n".join(pages)
    return text if re.sub(r"## Page \d+", "", text).strip() else ""


def pptx_text(details: dict[str, Any]) -> str:
    return "\n\n".join(f"## Slide {index + 1}\n" + "\n".join(slide) for index, slide in enumerate(details.get("slides", []) or []))


def pptx_details(data: bytes) -> dict[str, Any]:
    """Slide text of a PPTX, read with a hardened XML parser (called only inside the restricted worker)."""
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, recover=False)
    slides: list[list[str]] = []
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        names = sorted((n for n in package.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                       key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))  # type: ignore[union-attr]
        for name in names[:200]:
            root = etree.fromstring(package.read(name), parser)
            texts = [(node.text or "") for node in root.iter() if isinstance(node.tag, str) and node.tag.endswith("}t")]
            slides.append([t for t in texts if t])
    return {"slides": slides}
