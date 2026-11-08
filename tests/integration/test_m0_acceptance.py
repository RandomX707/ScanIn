from __future__ import annotations

import hashlib
import io
from pathlib import Path
from zipfile import ZipFile

import pytest
from openpyxl import load_workbook

from packages.core.adapters.xlsx.adapter import XlsxAdapter
from packages.core.models.contracts import Patch, TypedValue
from packages.core.planning.planner import build_patch_plan
from packages.core.verification.independent import IndependentVerifier
from tests.conftest import add_shared_strings, add_unsupported_cells, make_fixture


def prepared(tmp_path: Path, *, dependent: bool = False, date1904: bool = False) -> tuple[Path, bytes]:
    path = make_fixture(tmp_path / "gate.xlsx", dependent=dependent, date1904=date1904)
    add_shared_strings(path)
    add_unsupported_cells(path)
    return path, path.read_bytes()


def freeze(adapter, source, cell, value, sheet="Claim"):
    target = next(t for t in adapter.targets(source) if t.sheet == sheet and t.cell == cell)
    patch = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint, value)
    return build_patch_plan(adapter, source, [patch], provenance={"edit_set_id": "gate", "actor_id": "test", "created_at": "2026-11-06T00:00:00Z"})


def test_xlsx_1_and_8_empty_or_equal_edits_produce_no_derived_file(tmp_path):
    adapter = XlsxAdapter()
    path, source = prepared(tmp_path)
    original_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    empty = build_patch_plan(adapter, source, [], provenance={"edit_set_id": "empty"})
    equal = freeze(adapter, source, "G2", TypedValue("string", "constant"))
    assert adapter.apply(source, empty) is None
    assert adapter.apply(source, equal) is None
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_sha


def test_xlsx_2_one_of_two_shared_string_references_changes(tmp_path):
    adapter = XlsxAdapter()
    _, source = prepared(tmp_path)
    plan = freeze(adapter, source, "B2", TypedValue("string", "one changed"))
    candidate = adapter.apply(source, plan)
    report = IndependentVerifier().verify(source, candidate, plan)
    book = load_workbook(io.BytesIO(candidate))
    assert book["Claim"]["B2"].value == "one changed"
    assert book["Claim"]["C2"].value == "shared text"
    assert report.check("preservation").detail["shared_strings_unchanged"]
    assert report.outcome == "VERIFIED"


@pytest.mark.parametrize(("cell", "new_value", "expected"), [
    ("D2", TypedValue("decimal", 801.25), 801.25),
    ("E2", TypedValue("boolean", False), False),
    ("F2", TypedValue("date", "2026-11-06"), "2026-11-06"),
])
@pytest.mark.parametrize("date1904", [False, True])
def test_xlsx_3_currency_date_boolean_round_trip_and_style_is_unchanged(tmp_path, cell, new_value, expected, date1904):
    adapter = XlsxAdapter()
    _, source = prepared(tmp_path, date1904=date1904)
    plan = freeze(adapter, source, cell, new_value)
    candidate = adapter.apply(source, plan)
    assert candidate is not None
    report = IndependentVerifier().verify(source, candidate, plan)
    assert report.check("intent").status == "PASS"
    ref = next(t for t in adapter.targets(source) if t.cell == cell)
    book = load_workbook(io.BytesIO(candidate))
    assert str(book["Claim"][cell].style_id) == (ref.style_id or "0")
    actual = book["Claim"][cell].value
    assert (actual.isoformat()[:10] if hasattr(actual, "isoformat") else actual) == expected


def test_xlsx_4_known_and_cross_sheet_dependents_are_incomplete(tmp_path):
    adapter = XlsxAdapter()
    _, source = prepared(tmp_path, dependent=True)
    plan = freeze(adapter, source, "D2", TypedValue("decimal", 345.5))
    assert {"Claim!H2", "Claim!I2", "Totals!A1", "Totals!B1", "Totals!C1"} <= set(plan.dependents)
    assert plan.dependencies_unknown
    candidate = adapter.apply(source, plan)
    report = IndependentVerifier().verify(source, candidate, plan)
    assert report.outcome == "INCOMPLETE"
    assert report.check("dependents").status == "NOT_VERIFIED"
    assert report.check("dependents").detail["recalculation_requested"]


def test_xlsx_6_chart_image_comments_validations_formatting_and_relationships_preserved(tmp_path):
    adapter = XlsxAdapter()
    _, source = prepared(tmp_path)
    plan = freeze(adapter, source, "G2", TypedValue("string", "preserved"))
    candidate = adapter.apply(source, plan)
    report = IndependentVerifier().verify(source, candidate, plan)
    assert report.outcome == "VERIFIED"
    with ZipFile(io.BytesIO(source)) as old, ZipFile(io.BytesIO(candidate)) as new:
        changed = {name for name in old.namelist() if old.read(name) != new.read(name)}
        assert changed <= {p.part for p in plan.permitted_changes}
        assert old.read("xl/media/image1.png") == new.read("xl/media/image1.png")
        assert old.read("xl/drawings/drawing1.xml") == new.read("xl/drawings/drawing1.xml")
        assert old.read("xl/drawings/_rels/drawing1.xml.rels") == new.read("xl/drawings/_rels/drawing1.xml.rels")


def test_xlsx_9_indirect_dependency_is_unknown_and_never_verified(tmp_path):
    adapter = XlsxAdapter()
    _, source = prepared(tmp_path, dependent=True)
    plan = freeze(adapter, source, "D2", TypedValue("decimal", 799.0))
    candidate = adapter.apply(source, plan)
    report = IndependentVerifier().verify(source, candidate, plan)
    assert report.outcome == "INCOMPLETE"
    assert report.check("dependents").detail["unknown"]
