from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from zipfile import ZipFile

import pytest
from lxml import etree
from openpyxl import load_workbook

from packages.core.adapters.xlsx.adapter import XlsxAdapter
from packages.core.models.contracts import Patch, TypedValue
from packages.core.planning.planner import PlanError, build_patch_plan
from packages.core.verification.independent import IndependentVerifier
from tests.conftest import add_shared_strings, add_unsupported_cells, make_fixture


def target(adapter: XlsxAdapter, source: bytes, ref: str, sheet: str = "Claim"):
    return next(t for t in adapter.targets(source) + adapter.unsupported_targets(source) if t.sheet == sheet and t.cell == ref)


def create_plan(adapter: XlsxAdapter, source: bytes, ref: str, value: TypedValue, sheet: str = "Claim"):
    t = target(adapter, source, ref, sheet)
    return build_patch_plan(
        adapter,
        source,
        [Patch(source_version_id=hashlib.sha256(source).hexdigest(), target_id=t.id,
               expected_old_value=t.value, expected_fingerprint=t.fingerprint, new_value=value)],
        provenance={"edit_set_id": "m0-demo", "actor_id": "demo", "created_at": "2026-11-06T00:00:00Z"},
    )


def test_inspect_detects_features_and_list_targets(workbook_path: Path):
    adapter = XlsxAdapter()
    source = workbook_path.read_bytes()
    profile = adapter.inspect(source)
    assert profile.level == "editable"
    assert {"shared_strings", "formulas", "styles", "images", "charts"} <= set(profile.features)
    assert target(adapter, source, "B2").value.value == "shared text"
    assert not target(adapter, source, "H2").editable
    assert target(adapter, source, "H2").reason == "formula_cell"


def test_inspect_rejects_encrypted_and_marks_macro_workbooks_read_only(workbook_path: Path):
    adapter = XlsxAdapter()
    source = workbook_path.read_bytes()
    with ZipFile(__import__("io").BytesIO(source)) as zin:
        files = {i.filename: (i, zin.read(i.filename)) for i in zin.infolist()}
    files["xl/vbaProject.bin"] = (None, b"synthetic macro payload")
    types_info, types_bytes = files["[Content_Types].xml"]
    files["[Content_Types].xml"] = (types_info, types_bytes.replace(
        b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        b"application/vnd.ms-excel.sheet.macroEnabled.main+xml"))
    stream = __import__("io").BytesIO()
    with ZipFile(stream, "w") as zout:
        for name, (info, data) in files.items():
            zout.writestr(info or name, data, compress_type=info.compress_type if info else 8)
    profile = adapter.inspect(stream.getvalue())
    assert profile.level == "read_only"
    assert "macros_present" in profile.reasons
    assert adapter.inspect(b"\xd0\xcf\x11\xe0synthetic encrypted package").level == "rejected"


def test_patch_plan_is_frozen_and_preflight_rejects_stale_source(workbook_path: Path):
    adapter = XlsxAdapter()
    source = workbook_path.read_bytes()
    plan = create_plan(adapter, source, "G2", TypedValue("string", "updated"))
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        plan.source_sha256 = "tampered"  # type: ignore[misc]
    fresh = create_plan(adapter, source, "G2", TypedValue("string", "newer version"))
    changed = adapter.apply(source, fresh)
    assert changed is not None
    with pytest.raises(PlanError, match="stale_source"):
        build_patch_plan(adapter, changed, plan.patches, provenance=plan.provenance_dict())


def test_stale_target_fingerprint_is_rejected(workbook_path: Path):
    adapter = XlsxAdapter()
    source = workbook_path.read_bytes()
    t = target(adapter, source, "G2")
    patch = Patch(t.version_id, t.id, t.value, "0" * 64, TypedValue("string", "new"))
    with pytest.raises(PlanError, match="fingerprint_mismatch"):
        build_patch_plan(adapter, source, [patch], provenance={"edit_set_id": "stale-fingerprint"})


def test_shared_string_target_changes_without_touching_shared_table_or_other_cell(workbook_path: Path):
    adapter = XlsxAdapter()
    source = workbook_path.read_bytes()
    plan = create_plan(adapter, source, "B2", TypedValue("string", "private replacement"))
    output = adapter.apply(source, plan)
    wb = load_workbook(__import__("io").BytesIO(output), data_only=False)
    assert wb["Claim"]["B2"].value == "private replacement"
    assert wb["Claim"]["C2"].value == "shared text"
    before, after = ZipFile(__import__("io").BytesIO(source)), ZipFile(__import__("io").BytesIO(output))
    assert before.read("xl/sharedStrings.xml") == after.read("xl/sharedStrings.xml")


def test_safe_edit_verifies_and_preserves_unmodified_package_parts(workbook_path: Path):
    adapter = XlsxAdapter()
    source = workbook_path.read_bytes()
    plan = create_plan(adapter, source, "G2", TypedValue("string", "confirmed"))
    output = adapter.apply(source, plan)
    report = IndependentVerifier().verify(source, output, plan)
    assert report.outcome == "VERIFIED"
    assert all(check.status == "PASS" for check in report.checks)


def test_apply_is_deterministic_and_never_mutates_source(workbook_path: Path):
    adapter = XlsxAdapter()
    source = workbook_path.read_bytes()
    source_hash = hashlib.sha256(source).hexdigest()
    plan = create_plan(adapter, source, "G2", TypedValue("string", "repeatable"))
    first, second = adapter.apply(source, plan), adapter.apply(source, plan)
    assert first == second
    assert hashlib.sha256(source).hexdigest() == source_hash
    assert workbook_path.read_bytes() == source


def test_synthetic_fixture_bytes_are_reproducible(tmp_path: Path):
    first, second = tmp_path / "one.xlsx", tmp_path / "two.xlsx"
    make_fixture(first)
    make_fixture(second)
    add_shared_strings(first); add_unsupported_cells(first)
    add_shared_strings(second); add_unsupported_cells(second)
    assert first.read_bytes() == second.read_bytes()


def test_formula_dependent_edit_is_incomplete_and_calc_flagged(dependent_path: Path):
    adapter = XlsxAdapter()
    source = dependent_path.read_bytes()
    plan = create_plan(adapter, source, "D2", TypedValue("decimal", 200.5))
    assert "Claim!H2" in plan.dependents
    assert "Claim!I2" in plan.dependents
    assert "Totals!A1" in plan.dependents
    assert "Totals!B1" in plan.dependents
    output = adapter.apply(source, plan)
    report = IndependentVerifier().verify(source, output, plan)
    assert report.outcome == "INCOMPLETE"
    assert report.check("dependents").status == "NOT_VERIFIED"


def test_indirect_dependency_is_unknown_and_incomplete(dependent_path: Path):
    adapter = XlsxAdapter()
    source = dependent_path.read_bytes()
    plan = create_plan(adapter, source, "D2", TypedValue("decimal", 201))
    assert plan.dependencies_unknown
    output = adapter.apply(source, plan)
    assert IndependentVerifier().verify(source, output, plan).outcome == "INCOMPLETE"


def test_noop_returns_no_change_without_candidate(workbook_path: Path):
    adapter = XlsxAdapter()
    source = workbook_path.read_bytes()
    plan = create_plan(adapter, source, "G2", TypedValue("string", "constant"))
    assert plan.no_change
    result = adapter.apply(source, plan)
    assert result is None
    assert hashlib.sha256(source).hexdigest() == plan.source_sha256


def test_empty_edit_set_is_no_change(workbook_path: Path):
    source = workbook_path.read_bytes()
    plan = build_patch_plan(XlsxAdapter(), source, [], provenance={"edit_set_id": "empty"})
    assert plan.no_change
    assert XlsxAdapter().apply(source, plan) is None


def test_precision_limit_is_explicit(workbook_path: Path):
    adapter = XlsxAdapter()
    source = workbook_path.read_bytes()
    t = target(adapter, source, "G2")
    patch = Patch(t.version_id, t.id, t.value, t.fingerprint, TypedValue("integer", 1234567890123456))
    with pytest.raises(Exception, match="excel_numeric_precision_exceeded"):
        build_patch_plan(adapter, source, [patch], provenance={"edit_set_id": "precision"})


def test_incompatible_proposed_type_is_rejected_without_coercion(workbook_path: Path):
    adapter = XlsxAdapter()
    source = workbook_path.read_bytes()
    t = target(adapter, source, "G2")
    patch = Patch(t.version_id, t.id, t.value, t.fingerprint, TypedValue("boolean", True))
    with pytest.raises(PlanError, match="type_mismatch"):
        build_patch_plan(adapter, source, [patch], provenance={"edit_set_id": "type-mismatch"})


def test_formula_rich_text_array_targets_rejected_by_preflight(workbook_path: Path):
    adapter = XlsxAdapter()
    source = workbook_path.read_bytes()
    for ref, reason in (("H2", "formula_cell"), ("J2", "rich_text"), ("K2", "array_formula")):
        t = target(adapter, source, ref)
        patch = Patch(t.version_id, t.id, t.value, t.fingerprint, TypedValue("string", "unsafe"))
        with pytest.raises(PlanError) as exc:
            build_patch_plan(adapter, source, [patch], provenance={"edit_set_id": "unsafe"})
        assert exc.value.code == reason


def test_candidate_cannot_expand_frozen_change_manifest(workbook_path: Path):
    adapter = XlsxAdapter()
    source = workbook_path.read_bytes()
    plan = create_plan(adapter, source, "G2", TypedValue("string", "confirmed"))
    candidate = adapter.apply(source, plan)
    assert candidate is not None
    with ZipFile(__import__("io").BytesIO(candidate)) as z:
        members = {i.filename: (i, z.read(i.filename)) for i in z.infolist()}
    members["xl/styles.xml"] = (members["xl/styles.xml"][0], members["xl/styles.xml"][1] + b" ")
    poisoned = __import__("io").BytesIO()
    with ZipFile(poisoned, "w") as z:
        for info, data in members.values(): z.writestr(info, data, compress_type=info.compress_type)
    report = IndependentVerifier().verify(source, poisoned.getvalue(), plan)
    assert report.outcome == "BLOCKED"
    assert report.check("preservation").status == "FAIL"


def test_verifier_rejects_a_plan_changed_after_freeze(workbook_path: Path):
    adapter = XlsxAdapter()
    source = workbook_path.read_bytes()
    plan = create_plan(adapter, source, "G2", TypedValue("string", "confirmed"))
    candidate = adapter.apply(source, plan)
    tampered_plan = replace(plan, permitted_changes=())
    report = IndependentVerifier().verify(source, candidate, tampered_plan)
    assert report.outcome == "BLOCKED"
    assert report.check("policy").status == "FAIL"


def test_rejects_rich_text_and_array_formula_targets(workbook_path: Path):
    adapter = XlsxAdapter()
    source = workbook_path.read_bytes()
    unsupported = adapter.unsupported_targets(source)
    assert any(t.cell == "H2" and t.reason == "formula_cell" for t in unsupported)
    assert any(t.cell == "J2" and t.reason == "rich_text" for t in unsupported)
    assert any(t.cell == "K2" and t.reason == "array_formula" for t in unsupported)


@pytest.mark.parametrize("value", [TypedValue("integer", 1234567890123456), TypedValue("date", "2026-11-06"), TypedValue("boolean", False)])
def test_typed_values_round_trip_preserving_style(workbook_path: Path, value: TypedValue):
    adapter = XlsxAdapter()
    source = workbook_path.read_bytes()
    ref = {"integer": "L2", "date": "F2", "boolean": "E2"}[value.type]
    if value.type == "integer":
        value = TypedValue("integer", 123456789012345)
    plan = create_plan(adapter, source, ref, value)
    output = adapter.apply(source, plan)
    report = IndependentVerifier().verify(source, output, plan)
    assert report.outcome == "VERIFIED"
    with ZipFile(__import__("io").BytesIO(source)) as a, ZipFile(__import__("io").BytesIO(output)) as b:
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        for ref0 in (ref,):
            old = etree.fromstring(a.read("xl/worksheets/sheet1.xml")).xpath(f'.//m:c[@r="{ref0}"]', namespaces=ns)[0]
            new = etree.fromstring(b.read("xl/worksheets/sheet1.xml")).xpath(f'.//m:c[@r="{ref0}"]', namespaces=ns)[0]
            assert old.get("s") == new.get("s")
