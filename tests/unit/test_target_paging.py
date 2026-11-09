from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from packages.workers.parse_entry import MAX_TARGETS, ParserRejected, inspect_bytes


def _large_workbook(rows: int = 300, columns: int = 8) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Claims"
    for row in range(1, rows + 1):
        for column in range(1, columns + 1):
            sheet.cell(row, column, f"SYN-{row}-{column}")
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def test_targets_are_paged_without_losing_any_target():
    source = _large_workbook()
    first = inspect_bytes(source, filename="claims.xlsx", include_targets=True)
    assert len(first["targets"]) == MAX_TARGETS
    assert first["targets_truncated"] is True
    page = first["targets_page"]
    assert page == {"mode": "page", "offset": 0, "limit": MAX_TARGETS, "total": 2400, "next_offset": MAX_TARGETS}
    second = inspect_bytes(source, filename="claims.xlsx", include_targets=True, target_offset=page["next_offset"])
    assert len(second["targets"]) == 400 and second["targets_page"]["next_offset"] is None
    ids = [item["id"] for item in first["targets"] + second["targets"]]
    assert len(ids) == len(set(ids)) == 2400


def test_exact_lookup_returns_the_same_target_and_fingerprint_as_its_page():
    source = _large_workbook()
    paged = inspect_bytes(source, filename="claims.xlsx", include_targets=True, target_offset=MAX_TARGETS)
    deep = next(item for item in paged["targets"] if item["id"] == "xlsx:Claims!E290")
    lookup = inspect_bytes(source, filename="claims.xlsx", include_targets=True,
                           target_ids=["xlsx:Claims!E290", "xlsx:Claims!E105", "xlsx:Missing!A1"])
    assert lookup["targets_truncated"] is False
    assert lookup["targets_page"]["found"] == 2 and lookup["targets_page"]["total"] == 2400
    found = {item["id"]: item for item in lookup["targets"]}
    assert found["xlsx:Claims!E290"] == deep
    assert found["xlsx:Claims!E105"]["value"]["value"] == "SYN-105-5"


def test_small_documents_report_a_single_complete_page():
    result = inspect_bytes(b"claim_id,amount\nCLM-1,125\n", filename="claim.csv", include_targets=True)
    assert result["targets_truncated"] is False
    assert result["targets_page"]["next_offset"] is None and result["targets_page"]["total"] == len(result["targets"])


@pytest.mark.parametrize("kwargs", [
    {"target_offset": -1},
    {"target_offset": True},
    {"target_ids": ["x" * 513]},
    {"target_ids": [""]},
    {"target_ids": "xlsx:Claims!A1"},
    {"target_ids": [f"csv:{index}:1" for index in range(2001)]},
])
def test_unbounded_or_malformed_target_requests_fail_closed(kwargs):
    with pytest.raises(ParserRejected):
        inspect_bytes(b"claim_id,amount\nCLM-1,125\n", filename="claim.csv", include_targets=True, **kwargs)
