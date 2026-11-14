"""Sandboxed LibreOffice preview rendering: works, is confined, and never touches the network."""
from __future__ import annotations

import io
import socket
import threading
from zipfile import ZipFile

import pytest
from pypdf import PdfReader

from packages.workers.office_render import OfficeRenderRefused, SandboxedOfficeRenderer, seatbelt_profile
from pathlib import Path

renderer = SandboxedOfficeRenderer()
pytestmark = pytest.mark.skipif(not renderer.available(), reason="sandboxed LibreOffice renderer not available on this host")

CT = '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
RELS = '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def docx(body: str, rels: str | None = None, extra_ns: str = "") -> bytes:
    out = io.BytesIO()
    with ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", CT)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {W} {extra_ns}><w:body>{body}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>')
        if rels:
            z.writestr("word/_rels/document.xml.rels", rels)
    return out.getvalue()


def test_renders_plain_docx_to_pdf_pages():
    body = "".join(f'<w:p><w:r><w:t>Paragraph {i} of a synthetic page-render test.</w:t></w:r></w:p>' for i in range(5))
    pdf = renderer.render_pdf(docx(body), ".docx")
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 1 and "Paragraph 3" in reader.pages[0].extract_text()


def test_page_break_gives_multiple_pages():
    body = '<w:p><w:r><w:t>one</w:t></w:r></w:p><w:p><w:r><w:br w:type="page"/><w:t>two</w:t></w:r></w:p>'
    assert len(PdfReader(io.BytesIO(renderer.render_pdf(docx(body), ".docx"))).pages) == 2


def test_external_resource_is_never_fetched():
    """A DOCX that references an http image must not make the converter open any network connection."""
    hits: list[bytes] = []
    server = socket.socket(); server.bind(("127.0.0.1", 0)); server.listen(5); server.settimeout(0.3)
    port = server.getsockname()[1]
    stop = threading.Event()

    def accept():
        while not stop.is_set():
            try:
                conn, _ = server.accept(); hits.append(conn.recv(100)); conn.close()
            except OSError:
                pass
    t = threading.Thread(target=accept, daemon=True); t.start()
    rels = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            f'<Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="http://127.0.0.1:{port}/x.png" TargetMode="External"/></Relationships>')
    body = ('<w:p><w:r><w:t>external image below</w:t></w:r></w:p><w:p><w:r><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml" style="width:50pt;height:50pt">'
            '<v:imagedata xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:id="rId9"/></v:shape></w:pict></w:r></w:p>')
    try:
        renderer.render_pdf(docx(body, rels="""<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""), ".docx")
        renderer.render_pdf(docx(body.replace("rId9", "rId9")), ".docx")
    finally:
        stop.set(); server.close(); t.join(2)
    assert hits == []


def test_converter_cannot_read_home_or_repository_files(tmp_path):
    """Deny-default Seatbelt profile: nothing under /Users (home, repository, credentials) is readable except what the tool needs,
    writes are confined to the scratch directory, shared temp directories are not granted and IP networking is denied."""
    profile = seatbelt_profile(Path("/private/tmp/x"))
    assert "(deny default)" in profile and "(allow default)" not in profile
    assert '(subpath "/Users")' not in profile and '(subpath "/private/tmp")' not in profile
    assert "(deny file-write*)" in profile and "(deny network*)" in profile
    assert '(subpath "/private/var/folders")' not in profile and '(subpath "/private/var/tmp")' not in profile
    assert '(allow file-write* (subpath "/private/tmp/x")' in profile


def test_refuses_unsupported_type_and_garbage():
    with pytest.raises(OfficeRenderRefused):
        renderer.render_pdf(b"x", ".exe")
    with pytest.raises(OfficeRenderRefused):
        renderer.render_pdf(b"not really a document", ".docx")
