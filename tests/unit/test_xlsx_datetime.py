"""Phase 3B (N6): XLSX date, datetime and time-of-day cells keep their fractional day and native serial."""
from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import datetime, time
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from packages.core.adapters.xlsx.adapter import XlsxAdapter
from packages.core.models.contracts import Patch, TypedValue

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "xlsx" / "xlsx-05-dates-1904-formats.xlsx"  # QA XLSX-05 copy


def _targets(source: bytes) -> dict[str, object]:
    adapter = XlsxAdapter()
    return {t.id: t for t in adapter.targets(source) + adapter.unsupported_targets(source)}


def test_xlsx_05_datetime_keeps_its_time_and_serial():
    targets = _targets(FIXTURE.read_bytes())
    b3 = targets["xlsx:Dates!B3"]
    assert (b3.value.type, b3.value.value, b3.value.native) == ("datetime", "2026-10-21T21:40:00", "44854.90277777778")
    assert b3.editable is True and "+" not in b3.value.value and "Z" not in b3.value.value
    assert (targets["xlsx:Dates!B1"].value.type, targets["xlsx:Dates!B1"].value.value) == ("date", "2026-10-21")
    assert targets["xlsx:Dates!B4"].value.type == "decimal"          # a percentage is not a time


def _workbook(date1904: bool, cells: dict[str, tuple[object, str]]) -> bytes:
    workbook = Workbook()
    workbook.epoch = __import__("openpyxl").utils.datetime.MAC_EPOCH if date1904 else __import__("openpyxl").utils.datetime.WINDOWS_EPOCH
    sheet = workbook.active
    sheet.title = "D"
    for ref, (value, number_format) in cells.items():
        sheet[ref] = value
        sheet[ref].number_format = number_format
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


@pytest.mark.parametrize("date1904", [False, True])
def test_date_systems_types_and_read_only_times(date1904):
    source = _workbook(date1904, {
        "A1": (datetime(2026, 10, 21, 21, 40), "yyyy-mm-dd hh:mm"),
        "A2": (datetime(2026, 10, 21), "yyyy-mm-dd hh:mm"),
        "A3": (datetime(2026, 10, 21), "dd/mm/yyyy"),
        "A4": (datetime(2026, 10, 21, 6, 30, 15), "dd/mm/yyyy"),
        "A5": (time(3, 0), "h:mm"),
        "A6": (datetime(2026, 10, 21, 8, 0), "hh:mm"),
    })
    targets = _targets(source)
    observed = {ref: (targets[f"xlsx:D!{ref}"].value.type, targets[f"xlsx:D!{ref}"].value.value,
                      targets[f"xlsx:D!{ref}"].editable, targets[f"xlsx:D!{ref}"].reason) for ref in ("A1", "A2", "A3", "A4", "A5", "A6")}
    assert observed == {
        "A1": ("datetime", "2026-10-21T21:40:00", True, None),
        "A2": ("datetime", "2026-10-21T00:00:00", True, None),
        "A3": ("date", "2026-10-21", True, None),
        "A4": ("datetime", "2026-10-21T06:30:15", True, None),
        "A5": ("time", "03:00:00", False, "time_value_read_only"),
        "A6": ("datetime", "2026-10-21T08:00:00", False, "ambiguous_time_format"),
    }


def _patch_sheet(source: bytes, ref: bytes, raw: bytes) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                start = data.find(b'<c r="' + ref + b'"')
                v = data.find(b"<v>", start)
                data = data[:v + 3] + raw + data[data.find(b"</v>", v):]
            zout.writestr(info, data)
    return out.getvalue()


@pytest.mark.parametrize("raw", [b"60", b"60.5", b"-1"])
def test_ambiguous_serials_are_rejected_conservatively(raw):
    source = _patch_sheet(_workbook(False, {"A1": (datetime(2026, 1, 1), "yyyy-mm-dd")}), b"A1", raw)
    target = _targets(source)["xlsx:D!A1"]
    assert target.editable is False and target.reason == "ambiguous_excel_date_serial"


def _release(source: bytes, target_id: str, value: TypedValue):
    from packages.workflow.release import XlsxReleaseWorkflow
    adapter = XlsxAdapter()
    target = next(t for t in adapter.targets(source) if t.id == target_id)
    workflow = XlsxReleaseWorkflow()
    frozen = workflow.freeze(source=source, document_id="d", base_version_id="v", actor_id="owner", role="OWNER",
                             authorized_category="insurer-authored", reason="N6",
                             patches=(Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint, value),))
    return workflow.release(source=source, edit_set=frozen)


def _raw(source: bytes, ref: str) -> str:
    import re
    sheet = zipfile.ZipFile(io.BytesIO(source)).read("xl/worksheets/sheet1.xml").decode()
    return re.search(rf'<c r="{ref}"[^>]*>.*?<v>([^<]*)</v>', sheet).group(1)


def test_unrelated_release_leaves_the_datetime_untouched():
    source = FIXTURE.read_bytes()
    result = _release(source, "xlsx:Dates!B6", TypedValue("integer", 38501))
    assert result.status in {"VERIFIED", "STRUCTURALLY_VERIFIED"}, result.verification
    assert _raw(result.derived_bytes, "B3") == _raw(source, "B3") == "44854.90277777778"
    after = _targets(result.derived_bytes)["xlsx:Dates!B3"]
    assert (after.value.type, after.value.value) == ("datetime", "2026-10-21T21:40:00")
    reopened = load_workbook(io.BytesIO(result.derived_bytes))["Dates"]["B3"].value
    assert reopened == datetime(2026, 10, 21, 21, 40)


def test_datetime_edit_round_trips_in_the_1904_system():
    result = _release(FIXTURE.read_bytes(), "xlsx:Dates!B3", TypedValue("datetime", "2026-11-09T08:15:00"))
    assert result.status in {"VERIFIED", "STRUCTURALLY_VERIFIED"}, result.verification
    after = _targets(result.derived_bytes)["xlsx:Dates!B3"]
    assert (after.value.type, after.value.value) == ("datetime", "2026-11-09T08:15:00")
    assert load_workbook(io.BytesIO(result.derived_bytes))["Dates"]["B3"].value == datetime(2026, 11, 9, 8, 15)


@pytest.mark.parametrize("value,ok", [("08:15:00", True), ("23:59:59.250000", True), ("08:15:00+05:30", False),
                                      ("25:00", False), ("2026-11-09T08:15:00", False)])
def test_time_corrections_are_validated_and_never_carry_a_timezone(value, ok):
    from packages.api.documents import _typed_correction
    from packages.api.errors import APIError
    if ok:
        assert _typed_correction({"type": "time", "value": value}).value == value
    else:
        with pytest.raises(APIError):
            _typed_correction({"type": "time", "value": value})
