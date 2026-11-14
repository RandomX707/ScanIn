"""Deterministic section chunking and key-takeaway extraction (no AI).

A line-for-line port of the prototype's ``chunkify``/``enrich`` so the server produces the same sections
and takeaways the design shows. JavaScript ``\\s`` is spelled out explicitly because Python's differs.
"""
from __future__ import annotations

import re

_WS = "\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff"
_S = f"[{_WS}]"
_NS = f"[^{_WS}]"
_FLAGS = re.ASCII

_HEADING = re.compile(rf"^#{{1,4}}{_S}+(.+)$", _FLAGS)
_SECTION = re.compile(rf"^((?:section|chapter|article|part){_S}+\w+[.:]?{_S}+.+)$", _FLAGS | re.IGNORECASE)
_NUMBERED = re.compile(r"^(\d{1,2}[.)]" + _S + r"+[A-Za-z][^\n]{3,95})$", _FLAGS)
_PARA_SPLIT = re.compile(rf"\n{_S}*\n")
_SENT_BIG = re.compile(r"[^.!?]+[.!?]+|[^.!?]+$")
_SENT = re.compile(r"[^.!?]+(?:[.!?]+|$)")
_WS_RUN = re.compile(f"{_S}+")
_PIPE = re.compile(rf"{_S}*\|{_S}*")
_KEYWORDS = re.compile(
    r"\b(must|within|limit|require|covered|eligible|maximum|exclusion|terminate|expires?|only|at least|should|"
    r"not covered|renew|before|after|days?|hours?|\d[\d,.]*%?|INR|USD|\$)\b", _FLAGS | re.IGNORECASE)
_TAIL_WORD = re.compile(rf"{_S}+{_NS}*$")
_METADATA_KEYS = ("Policy reference", "Effective period", "Prepared for")


def _strip(value: str) -> str:
    return re.sub(f"^{_S}+|{_S}+$", "", value)


def _paragraphs(text: str) -> list[str]:
    return [part for part in _PARA_SPLIT.split(text) if part]


def chunkify(text: str | None) -> list[dict[str, object]]:
    source = _strip(str(text or "").replace("\r\n", "\n"))
    if not source:
        return []
    sections: list[dict[str, str]] = []
    current: dict[str, object] = {"title": "Overview", "content": []}

    def push() -> None:
        body = _strip("\n".join(current["content"]))  # type: ignore[arg-type]
        if body:
            sections.append({"title": str(current["title"]), "body": body})

    for line in source.split("\n"):
        stripped = _strip(line)
        match = _HEADING.match(stripped) or _SECTION.match(stripped) or _NUMBERED.match(stripped)
        if match:
            push()
            current = {"title": _strip(match.group(1)), "content": []}
        else:
            current["content"].append(line)  # type: ignore[union-attr]
    push()
    if not sections:
        sections.append({"title": "Document overview", "body": source})

    final: list[dict[str, str]] = []
    for section in sections:
        if len(section["body"]) <= 1550:
            final.append(section)
            continue
        buffer, part = "", 0

        def title() -> str:
            return f"{section['title']} · continued {part + 1}" if part else section["title"]

        for paragraph in _paragraphs(section["body"]):
            if buffer and len(buffer) + len(paragraph) > 1150:
                final.append({"title": title(), "body": _strip(buffer)})
                part += 1
                buffer = ""
            if len(paragraph) > 1550:
                for sentence in _SENT_BIG.findall(paragraph) or [paragraph]:
                    if buffer and len(buffer) + len(sentence) > 1100:
                        final.append({"title": title(), "body": _strip(buffer)})
                        part += 1
                        buffer = ""
                    buffer += _strip(sentence) + " "
            else:
                buffer += paragraph + "\n\n"
        if _strip(buffer):
            final.append({"title": title(), "body": _strip(buffer)})

    if len(final) == 1 and len(final[0]["body"]) > 1400:
        paragraphs = _paragraphs(source)
        if len(paragraphs) > 1:
            result: list[dict[str, str]] = []
            bucket = ""
            for paragraph in paragraphs:
                if bucket and len(bucket) + len(paragraph) > 1150:
                    result.append({"title": f"Reading section {len(result) + 1:02d}", "body": _strip(bucket)})
                    bucket = ""
                bucket += paragraph + "\n\n"
            if _strip(bucket):
                result.append({"title": f"Reading section {len(result) + 1:02d}", "body": _strip(bucket)})
            return enrich(result)
    return enrich(final)


def _shorten(value: str, limit: int = 170) -> str:
    if len(value) <= limit:
        return value
    return _TAIL_WORD.sub("", value[: limit - 1]) + "…"


def enrich(sections: list[dict[str, str]]) -> list[dict[str, object]]:
    enriched: list[dict[str, object]] = []
    for index, section in enumerate(sections):
        body, title = section["body"], section["title"]
        normalized = _strip(_PIPE.sub(" · ", _WS_RUN.sub(" ", body)))
        sentences = [item for item in (_strip(s) for s in _SENT.findall(normalized)) if 24 <= len(item) < 460]
        scored = [(sentence, j, (3 if _KEYWORDS.search(sentence) else 0) + (2 if len(sentence) < 205 else 0) + (1 if j == 0 else 0))
                  for j, sentence in enumerate(sentences)]
        scored.sort(key=lambda item: (-item[2], item[1]))
        important = sorted(scored[:3], key=lambda item: item[1])
        highlights = [item for item in (_shorten(s) for s, _, _ in important) if item]
        short = "".join(" ".join(highlights[:2]))[:300] or _shorten(normalized, 260)
        metadata = []
        for key in _METADATA_KEYS:
            found = re.search(rf"^{re.escape(key)}:{_S}*(.+)$", body, _FLAGS | re.IGNORECASE | re.MULTILINE)
            if found and found.group(1):
                metadata.append((key, found.group(1)))
        if len(metadata) >= 2:
            highlights = [f"{key}: {value}" for key, value in metadata]
            short = f"Document snapshot — {title}. " + " · ".join(f"{key}: {value}" for key, value in metadata)
        elif re.match(r"^(Table|Sheet):", title, re.IGNORECASE):
            rows = [row for row in body.split("\n") if _strip(row)]
            headings = [h for h in (_strip(part) for part in (rows[0] if rows else "").split("|")) if h]
            short = (f"Structured dataset with {max(0, len(rows) - 1)} records"
                     f"{', covering ' + ', '.join(headings[:5]) if headings else ''}.")
            highlights = [f"{len(rows)} rows including headings",
                          "Fields: " + " · ".join(headings) if headings else "Open the original to inspect the complete data"]
        elif re.match(r"^Slide \d+", title, re.IGNORECASE):
            lines = [line for line in (_strip(x) for x in body.split("\n")) if line]
            short = "This slide covers " + " · ".join(lines[:4])[:260] + "."
            highlights = lines[:3]
        stripped_body = _strip(body)
        words = len(_WS_RUN.split(stripped_body)) if stripped_body else 0
        enriched.append({"id": f"c{index + 1}", "title": title, "body": body, "summary": short, "words": words,
                         "points": highlights[:3], "method": "Key statements summarised from document text"})
    return enriched
