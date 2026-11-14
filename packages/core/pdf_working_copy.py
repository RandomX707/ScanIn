"""Build an UNSIGNED, visibly marked working copy of a (digitally signed) PDF.

Runs inside the sandboxed PDF worker. The signed original is never touched: this function only reads its bytes.

What the copy is:    a fresh, fully rewritten PDF with every signature field, signature value, DocMDP permission, signature
                     flag and document security store removed, so it carries NO cryptographic signature - valid, invalid or
                     lookalike - and cannot be mistaken for the signed original.
What the copy shows: a permanent strip above every page ("DRAFT — MODIFIED COPY — NOT OFFICIAL" and a provenance line) added
                     in NEW space (the page grows upward), so no original content is covered or moved.
What it never does:  copy a cryptographic signature, fabricate one, or claim the signer approved anything.
The original page appearance of a signature (a stamped image, a logo, a drawn box in page content) is left as page content;
the strip and the provenance metadata make clear it is NOT a signature of this document.
"""
from __future__ import annotations

import io
import re
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject

MARKER = "DRAFT — MODIFIED COPY — NOT OFFICIAL"
STRIP_HEIGHT = 26.0
FORBIDDEN = (b"/ByteRange", b"/DocMDP", b"/SigFlags", b"/DSS", b"/Type/Sig", b"/Type /Sig", b"/FT/Sig", b"/FT /Sig")


class WorkingCopyRefused(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _is_sig_field(obj: Any) -> bool:
    try:
        node = obj.get_object() if hasattr(obj, "get_object") else obj
        seen = 0
        while node is not None and seen < 8:
            if node.get("/FT") == "/Sig":
                return True
            value = node.get("/V")
            if value is not None:
                v = value.get_object()
                if isinstance(v, DictionaryObject) and v.get("/Type") == "/Sig":
                    return True
            node = node["/Parent"].get_object() if "/Parent" in node else None
            seen += 1
    except Exception:  # noqa: BLE001 - unreadable structure: treat as a signature artefact and drop it
        return True
    return False


def _strip_signatures(writer: PdfWriter) -> int:
    removed = 0
    root = writer._root_object  # noqa: SLF001
    for key in ("/Perms", "/DSS"):
        if key in root:
            del root[key]; removed += 1
    acro = root.get("/AcroForm")
    if acro is not None:
        acro = acro.get_object()
        if "/SigFlags" in acro:
            del acro["/SigFlags"]
        fields = acro.get("/Fields")
        if fields is not None:
            kept = ArrayObject()
            for field in fields.get_object():
                if _is_sig_field(field):
                    removed += 1
                else:
                    kept.append(field)
            acro[NameObject("/Fields")] = kept
    for page in writer.pages:
        annots = page.get("/Annots")
        if annots is None:
            continue
        kept = ArrayObject()
        for annot in annots.get_object():
            if annot.get_object().get("/Subtype") == "/Widget" and _is_sig_field(annot):
                removed += 1
            else:
                kept.append(annot)
        page[NameObject("/Annots")] = kept
    return removed


def _strip_pdf(width: float, text_lines: tuple[str, str]) -> bytes:
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(width, STRIP_HEIGHT))
    c.setFillColorRGB(0.62, 0.05, 0.08)
    c.rect(0, 0, width, STRIP_HEIGHT, stroke=0, fill=1)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 10.5)
    c.drawString(10, STRIP_HEIGHT - 11.5, text_lines[0])
    c.setFont("Helvetica", 6.4)
    provenance = text_lines[1]
    while c.stringWidth(provenance, "Helvetica", 6.4) > width - 20 and len(provenance) > 20:
        provenance = provenance[:-2]
    c.drawString(10, 5.5, provenance)
    c.showPage(); c.save()
    return buffer.getvalue()


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def build(source: bytes, *, name: str, source_sha256: str, source_state: str) -> tuple[bytes, dict[str, Any]]:
    try:
        reader = PdfReader(io.BytesIO(source), strict=False)
    except Exception:  # noqa: BLE001
        raise WorkingCopyRefused("working_copy_unreadable") from None
    if reader.is_encrypted:
        raise WorkingCopyRefused("working_copy_encrypted")
    page_count = len(reader.pages)
    if not 1 <= page_count <= 1000:
        raise WorkingCopyRefused("working_copy_page_count")
    first = PdfWriter(clone_from=reader)
    removed = _strip_signatures(first)
    # Unlinked signature objects are still in the first writer's object table. Re-read what it writes and clone again:
    # only objects reachable from the cleaned catalogue survive, so no signature dictionary can be written.
    staged = io.BytesIO(); first.write(staged)
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(staged.getvalue()), strict=False))
    provenance = (f"Unsigned working copy of \"{name[:70]}\" · NOT issued, signed or approved by the original signer · "
                  f"source SHA-256 {source_sha256[:16]}…")
    for page in writer.pages:
        box = page.mediabox
        left, bottom, right, top = float(box.left), float(box.bottom), float(box.right), float(box.top)
        for attr in ("mediabox", "cropbox"):
            target = getattr(page, attr)
            target.upper_right = (float(target.right), float(target.top) + STRIP_HEIGHT)
        for key in ("/TrimBox", "/BleedBox", "/ArtBox"):
            if key in page:
                del page[key]
        stamp = PdfReader(io.BytesIO(_strip_pdf(right - left, (MARKER, provenance)))).pages[0]
        from pypdf import Transformation
        page.merge_transformed_page(stamp, Transformation().translate(left, top))
    info = {"/Subject": TextStringObject("Unsigned working copy — not official"),
            "/Keywords": TextStringObject("Channel360 unsigned working copy; modified copy; not official"),
            "/Producer": TextStringObject("Channel360 working copy"),
            "/C360WorkingCopy": TextStringObject("true"), "/C360SourceSHA256": TextStringObject(source_sha256),
            "/C360SourceSignatureState": TextStringObject(source_state)}
    writer.add_metadata(info)
    out = io.BytesIO()
    writer.write(out)
    data = out.getvalue()
    return data, verify(source, data, removed=removed, source_pages=page_count)


def verify(source: bytes, data: bytes, *, removed: int, source_pages: int) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
    if not data.startswith(b"%PDF-"):
        raise WorkingCopyRefused("working_copy_verification_failed")
    flat = data.replace(b"\n", b"").replace(b"\r", b"")
    leftovers = [m.decode() for m in FORBIDDEN if m in flat]
    add("no_signature_structures", not leftovers, ",".join(leftovers))
    try:
        rd = PdfReader(io.BytesIO(data), strict=True)
        src = PdfReader(io.BytesIO(source), strict=False)
    except Exception:  # noqa: BLE001
        raise WorkingCopyRefused("working_copy_verification_failed") from None
    add("page_count_preserved", len(rd.pages) == source_pages, f"{len(rd.pages)}/{source_pages}")
    marker_missing, text_changed = [], []
    for i, page in enumerate(rd.pages):
        text = page.extract_text() or ""
        if _norm(MARKER) not in _norm(text):
            marker_missing.append(i + 1)
        original = _norm(src.pages[i].extract_text() or "").replace(_norm(MARKER), "")     # a source that is itself a working copy already carries the notice
        without_strip = _norm(text).replace(_norm(MARKER), "")
        # the provenance line is also stamped text; the original page text must still be present, unchanged and in order
        if original and original not in without_strip:
            text_changed.append(i + 1)
    add("marker_on_every_page", not marker_missing, ",".join(map(str, marker_missing[:10])))
    add("original_page_text_preserved", not text_changed, ",".join(map(str, text_changed[:10])))
    meta = rd.metadata or {}
    add("provenance_metadata", str(meta.get("/C360WorkingCopy")) == "true")
    ok = all(c["ok"] for c in checks)
    report = {"outcome": "VERIFIED" if ok else "FAILED", "checks": checks, "signature_artefacts_removed": removed, "pages": len(rd.pages)}
    if not ok:
        raise WorkingCopyRefused("working_copy_verification_failed")
    return report
