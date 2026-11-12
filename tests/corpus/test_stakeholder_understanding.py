"""No fabricated content: every extracted fact/heading must be found at the source location it cites (private corpus)."""
import glob
import io
import os
import re
from pathlib import Path

import openpyxl
import pypdfium2 as pdfium
import pytest

from packages.core import policy_sections as P
from packages.core import sheet_sections as S
from packages.core.adapters.xlsx.adapter import XlsxAdapter
from packages.core.xlsx_layout import extract
from packages.workers.pdf_edit_entry import outline

ROOT = Path(__file__).resolve().parents[2]
CORPUS = Path(os.environ.get("CHANNEL360_CORPUS_DIR", ROOT / "Policy Copies"))
EXTRACTED = ROOT / ".private/corpus"
PDFS = ["CAR Policy copy.pdf", "Fire Policy copy.pdf", "GMC-Policy copy.pdf", "Health Policy copy.pdf", "Marine Policy copy.pdf", "Motor  Policy.pdf"]


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def _all_tokens_present(text: str, page: str) -> bool:
    """Every word (3+ letters/digits) of a shown string must exist on the cited page, so nothing can be invented."""
    return all(_norm(tok) in page for tok in re.findall(r"[A-Za-z0-9]{3,}", text))


def _pdf(name: str) -> bytes:
    path = CORPUS / name if not name.startswith("QCR/") else EXTRACTED / "QCR" / name
    if not path.exists():
        pytest.skip("private stakeholder corpus not present")
    return path.read_bytes()


@pytest.mark.parametrize("name", PDFS + ["QCR/QCR Health.pdf"])
def test_pdf_facts_and_headings_exist_on_the_cited_page(name):
    data = _pdf(name)
    doc = pdfium.PdfDocument(data)
    page_text = {i + 1: _norm(doc[i].get_textpage().get_text_range()) for i in range(len(doc))}
    result = P.segment(outline(data))
    assert result["sections"], "a policy document must yield at least one supported section"
    assert len(result["sections"]) <= 12
    for g in result["sections"]:
        assert g["occurrences"] or g["facts"]
        for occ in g["occurrences"]:
            assert "\ufffd" not in occ["heading"] + occ["excerpt"]
            assert _all_tokens_present(occ["heading"], page_text[occ["page"]]), f"heading words not on page {occ['page']}"
            if occ["excerpt"]:
                assert _all_tokens_present(occ["excerpt"].rstrip("…"), page_text[occ["excerpt_page"]]), "excerpt words not on the cited page"
        for fact in g["facts"]:
            assert _norm(fact["value"]) in page_text[fact["page"]] or all(
                _norm(tok) in page_text[fact["page"]] for tok in re.findall(r"[A-Za-z0-9]{3,}", fact["value"])), f"fact value not on page {fact['page']}"
            assert _norm(fact["as_written"]) in page_text[fact["page"]]


def _books():
    return sorted({Path(p) for pat in ("QCR/*", "RFQ/RFQ/*", "RFQ/QCR/*") for p in glob.glob(str(EXTRACTED / pat)) if p.lower().endswith(".xlsx")})


def test_spreadsheet_facts_equal_the_cells_they_cite():
    books = _books()
    if not books:
        pytest.skip("private stakeholder corpus not present")
    checked = 0
    for path in books:
        data = path.read_bytes()
        wb = openpyxl.load_workbook(io.BytesIO(data))
        layout = extract(data)
        targets = XlsxAdapter().targets(data)
        for ws, lay in zip(wb.worksheets, layout["sheets"]):
            rows: list[list] = []
            cells = {t.cell: t for t in targets if t.sheet == ws.title}
            from openpyxl.utils.cell import coordinate_from_string, column_index_from_string
            mr = mc = 0
            for ref in cells:
                col, row = coordinate_from_string(ref)
                mr, mc = max(mr, row), max(mc, column_index_from_string(col))
            for r in range(1, mr + 1):
                line = []
                for c in range(1, min(mc, 40) + 1):
                    t = cells.get(f"{openpyxl.utils.get_column_letter(c)}{r}")
                    if t is None:
                        line.append(None)
                    else:
                        v = t.value.value
                        line.append({"v": "" if v is None else str(v), "t": t.value.type, "e": t.editable})
                rows.append(line)
            for chunk in S.sheet_chunks({"name": ws.title, "rows": rows, "layout": lay}, layout["styles"]):
                assert chunk["facts"] or chunk["points"]
                for fact in chunk["facts"]:
                    cell_v = ws[fact["cell"]].value
                    if hasattr(cell_v, "date"):                       # openpyxl returns datetimes; the adapter reports ISO dates
                        cell_v = cell_v.date().isoformat() if hasattr(cell_v, "hour") else cell_v.isoformat()
                    assert (fact["value"] == "" and cell_v in (None, "")) or str(cell_v).strip() == fact["value"].strip() or fact["formula"], (path.name, fact["cell"])
                    assert str(ws[fact["label_cell"]].value or "").strip().rstrip(": ") == fact["label"] or " · " in fact["label"], (path.name, fact["label_cell"])
                    checked += 1
    assert checked > 100
