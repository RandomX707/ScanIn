"""Rendered-HTML editing of Markdown and HTML documents.

The Edits tab shows the rendered document and edits it in place. Saving serialises the edited HTML back to the
original format (Markdown with a real serialiser, HTML sanitised with nh3) and refuses the save unless the rendered
text of every block that was not edited is unchanged.
"""
from __future__ import annotations

import difflib
import re

import nh3
from lxml import html as lxml_html
from markdown_it import MarkdownIt
from markdownify import MarkdownConverter

TAGS = {"p", "br", "strong", "b", "em", "i", "u", "s", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
        "blockquote", "pre", "code", "hr", "a", "table", "thead", "tbody", "tr", "th", "td", "sub", "sup", "div", "span"}
ATTRIBUTES = {"a": {"href"}, "td": {"colspan", "rowspan"}, "th": {"colspan", "rowspan"}}
SCHEMES = {"http", "https", "mailto"}
BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "blockquote", "td", "th", "div"}


class RichTextRejected(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def sanitize(html: str) -> str:
    return nh3.clean(html, tags=TAGS, attributes=ATTRIBUTES, url_schemes=SCHEMES, link_rel="noopener noreferrer")


_markdown = MarkdownIt("commonmark", {"html": True, "breaks": True}).enable(["table", "strikethrough"])


def render_markdown(source: str) -> str:
    return sanitize(_markdown.render(source))


class _Converter(MarkdownConverter):
    def convert_u(self, el, text, parent_tags):  # Markdown has no underline: keep it as inline HTML
        return f"<u>{text}</u>" if text.strip() else text


def html_to_markdown(html: str) -> str:
    out = _Converter(heading_style="ATX", bullets="-", strong_em_symbol="*", newline_style="spaces",
                     escape_asterisks=True, escape_underscores=True).convert(sanitize(html))
    out = out.replace("  \n", "\n")  # the renderer treats a newline inside a paragraph as a line break
    return re.sub(r"\n{3,}", "\n\n", out).strip("\n") + "\n"


def split_body(source: str) -> tuple[str, str, str]:
    """(before, inner, after) of a full HTML document's body; fragments are all ``inner``."""
    found = re.search(r"(?is)(<body\b[^>]*>)(.*?)(</body\s*>)", source)
    if not found:
        return "", source, ""
    return source[: found.start(2)], found.group(2), source[found.end(2):]


def render_html_document(source: str) -> str:
    return sanitize(split_body(source)[1])


def block_texts(html: str) -> list[str]:
    """Whitespace-normalised rendered text of every leaf block, in document order."""
    root = lxml_html.fragment_fromstring(f"<div>{html}</div>")
    texts: list[str] = []
    for el in root.iter():
        if not isinstance(el.tag, str) or el is root:
            continue
        if el.tag in BLOCKS and not any(isinstance(c.tag, str) and c.tag in BLOCKS for c in el.iterdescendants()):
            text = re.sub(r"\s+", " ", el.text_content()).strip()
            if text:
                texts.append(text)
    # Text directly inside the wrapper (no block element) still counts.
    loose = re.sub(r"\s+", " ", (root.text or "") + "".join((c.tail or "") for c in root)).strip()
    if loose:
        texts.insert(0, loose)
    return texts


def serialise(fmt: str, original: str, edited_html: str) -> str:
    clean = sanitize(edited_html)
    if fmt == "md":
        return html_to_markdown(clean)
    before, inner, after = split_body(original)
    body = clean.strip()
    return f"{before}\n{body}\n{after}" if (before or after) else body + ("\n" if original.endswith("\n") else "")


def check_untouched_unchanged(fmt: str, original: str, new_source: str, edited_html: str) -> None:
    """Reject the save unless every untouched block renders to the same text before and after."""
    render = render_markdown if fmt == "md" else render_html_document
    before = block_texts(render(original))
    edited = block_texts(sanitize(edited_html))
    after = block_texts(render(new_source))
    if after != edited:
        raise RichTextRejected("formatting_not_preserved")
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=before, b=edited, autojunk=False).get_opcodes():
        if tag == "equal" and after[j1:j2] != before[i1:i2]:
            raise RichTextRejected("formatting_not_preserved")
