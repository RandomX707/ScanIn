from __future__ import annotations

import io
import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from zipfile import ZipFile

import pytest

from packages.core.adapters.xlsx.adapter import XlsxAdapter
from packages.core.models.contracts import TypedValue
from packages.core.verification.independent import IndependentVerifier
from tests.unit.test_xlsx_engine import create_plan
from tests.unit.test_fixture_png import _validate_png


def _rewrite_part(candidate: bytes, part: str, change) -> bytes:
    with ZipFile(io.BytesIO(candidate)) as archive:
        members = {info.filename: (info, archive.read(info.filename)) for info in archive.infolist()}
    info, data = members[part]
    members[part] = (info, change(data))
    result = io.BytesIO()
    with ZipFile(result, "w") as archive:
        for member_info, member_data in members.values():
            archive.writestr(member_info, member_data, compress_type=member_info.compress_type)
    return result.getvalue()


def _mutate_xml(data: bytes, action) -> bytes:
    root = ET.fromstring(data)
    action(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _change_cell_value(ref: str):
    def mutate(root):
        main = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        cell = next(n for n in root.iter(main + "c") if n.get("r") == ref)
        inline = cell.find(main + "is")
        if inline is not None:
            inline.find(main + "t").text = "tampered unrelated value"
        else:
            cell.find(main + "v").text = "999999"
    return lambda data: _mutate_xml(data, mutate)


@pytest.fixture
def authorized_candidate(workbook_path):
    adapter = XlsxAdapter()
    source = workbook_path.read_bytes()
    plan = create_plan(adapter, source, "G2", TypedValue("string", "authorized"))
    candidate = adapter.apply(source, plan)
    assert candidate is not None
    return source, candidate, plan


@pytest.mark.parametrize(("label", "part", "change", "check"), [
    ("unrelated worksheet cell", "xl/worksheets/sheet1.xml", _change_cell_value("A1"), "preservation"),
    ("formula", "xl/worksheets/sheet1.xml", lambda data: _mutate_xml(data, lambda root: next(
        n for n in root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}f")
    ).__setattr__("text", "D2*77")), "preservation"),
    ("target style reference", "xl/worksheets/sheet1.xml", lambda data: _mutate_xml(data, lambda root: next(
        n for n in root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c") if n.get("r") == "G2"
    ).set("s", "1")), "intent"),
    ("OOXML relationship", "xl/drawings/_rels/drawing1.xml.rels", lambda data: _mutate_xml(data, lambda root: next(iter(root)).set("Id", "rIdTampered")), "preservation"),
    ("embedded image", "xl/media/image1.png", lambda data: data[:-1] + bytes([data[-1] ^ 1]), "preservation"),
    ("chart part", "xl/charts/chart1.xml", lambda data: data + b" ", "preservation"),
])
def test_candidate_mutations_outside_targeted_authorization_are_blocked(authorized_candidate, label, part, change, check):
    source, candidate, plan = authorized_candidate
    mutated = _rewrite_part(candidate, part, change)
    report = IndependentVerifier().verify(source, mutated, plan)
    assert report.outcome == "BLOCKED", label
    assert report.check(check).status == "FAIL", label


def test_existing_shared_string_mutation_is_blocked(authorized_candidate):
    source, candidate, plan = authorized_candidate
    def change(data):
        return _mutate_xml(data, lambda root: root.find(
            "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si"
        ).find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t").__setattr__("text", "tampered shared value"))
    report = IndependentVerifier().verify(source, _rewrite_part(candidate, "xl/sharedStrings.xml", change), plan)
    assert report.outcome == "BLOCKED"
    assert report.check("preservation").status == "FAIL"


def test_independent_verifier_rejects_modification_of_valid_embedded_png(authorized_candidate):
    source, candidate, plan = authorized_candidate
    with ZipFile(io.BytesIO(source)) as package:
        original_image = package.read("xl/media/image1.png")
    _validate_png(original_image)
    mutated = _rewrite_part(candidate, "xl/media/image1.png", lambda data: data[:-1] + bytes([data[-1] ^ 1]))
    report = IndependentVerifier().verify(source, mutated, plan)
    assert report.outcome == "BLOCKED"
    assert report.check("preservation").status == "FAIL"


def test_provenance_metadata_mutation_is_blocked(authorized_candidate):
    source, candidate, plan = authorized_candidate
    def change(data):
        def modify(root):
            prop = next(n for n in root if n.get("name") == "IDW_M0_Provenance")
            value = json.loads(prop[0].text)
            value["actor_id"] = "attacker"
            prop[0].text = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return _mutate_xml(data, modify)
    report = IndependentVerifier().verify(source, _rewrite_part(candidate, "docProps/custom.xml", change), plan)
    assert report.outcome == "BLOCKED"
    assert report.check("provenance").status == "FAIL"


def test_frozen_plan_permission_mutation_is_blocked(authorized_candidate):
    source, candidate, plan = authorized_candidate
    changed_permissions = replace(plan, permitted_changes=tuple())
    report = IndependentVerifier().verify(source, candidate, changed_permissions)
    assert report.outcome == "BLOCKED"
    assert report.check("policy").status == "FAIL"


def test_verifier_does_not_call_writer_parsing_or_fingerprint_helpers(authorized_candidate, monkeypatch):
    source, candidate, plan = authorized_candidate
    def forbidden(*args, **kwargs):
        raise AssertionError("writer helper invoked by verifier")
    monkeypatch.setattr(XlsxAdapter, "_data", forbidden)
    monkeypatch.setattr(XlsxAdapter, "_fingerprint", forbidden, raising=False)
    report = IndependentVerifier().verify(source, candidate, plan)
    assert report.outcome == "VERIFIED"
