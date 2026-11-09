"""Phase 3A item 3: inspection reports a document-wide editable target count, independent of the page."""
from __future__ import annotations

import io

from openpyxl import Workbook

from packages.workers.parse_entry import MAX_TARGETS, inspect_bytes
from tests.unit.test_readonly_capabilities import _rewrite, _rich_workbook, _workbook


def _csv(rows: int) -> bytes:
    return b"claim,name,amount,status\n" + b"".join(f"C{i},N{i},{i},open\n".encode() for i in range(rows))


def test_paged_csv_reports_the_document_total_not_the_page():
    source = _csv(1100)
    first = inspect_bytes(source, filename="c.csv", include_targets=True)
    later = inspect_bytes(source, filename="c.csv", include_targets=True, target_offset=MAX_TARGETS)
    lookup = inspect_bytes(source, filename="c.csv", include_targets=True, target_ids=["csv:2:2"])
    assert len(first["targets"]) == MAX_TARGETS and first["targets_page"]["total"] == 4404
    assert first["targets_editable_total"] == later["targets_editable_total"] == lookup["targets_editable_total"] == 4404


def test_read_only_or_mixed_documents_have_no_editable_targets():
    assert inspect_bytes(b"a,b\r\nc,d\ne,f\r\n", filename="m.csv", include_targets=True)["targets_editable_total"] == 0
    macro = _rewrite(_workbook(), lambda name, data: data, {"xl/vbaProject.bin": b"x"})
    assert inspect_bytes(macro, filename="m.xlsm", include_targets=True)["targets_editable_total"] == 0
    assert inspect_bytes(b"claim,name\nC1,x\x81y\n", filename="u.csv", include_targets=True)["targets_editable_total"] == 0


def test_rich_text_cells_are_excluded_from_the_editable_total():
    result = inspect_bytes(_rich_workbook(), filename="r.xlsx", include_targets=True)
    assert result["targets_editable_total"] == sum(1 for t in result["targets"] if t["editable"]) == len(result["targets"]) - 2


def test_no_total_without_targets():
    assert "targets_editable_total" not in inspect_bytes(_csv(3), filename="c.csv")
