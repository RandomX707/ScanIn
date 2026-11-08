from __future__ import annotations

import hashlib
import io
from zipfile import ZipFile

from packages.core.adapters.xlsx.adapter import XlsxAdapter
from packages.core.models.contracts import Patch, TypedValue
from packages.core.planning.planner import build_patch_plan
from packages.core.verification.independent import IndependentVerifier
from tests.conftest import add_shared_strings, add_unsupported_cells, make_fixture
from tests.unit.test_fixture_png import _validate_png


def _prepare(tmp_path, *, dependent: bool):
    path = make_fixture(tmp_path / ("dependent.xlsx" if dependent else "safe.xlsx"), dependent=dependent)
    add_shared_strings(path)
    add_unsupported_cells(path)
    return path, path.read_bytes()


def _make_plan(source: bytes, *, cell: str, value: TypedValue):
    adapter = XlsxAdapter()
    target = next(item for item in adapter.targets(source) if item.sheet == "Claim" and item.cell == cell)
    patch = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint, value)
    return build_patch_plan(adapter, source, [patch], provenance={
        "edit_set_id": "png-preservation", "actor_id": "test", "created_at": "2026-11-06T00:00:00Z"
    })


def _package_parts(data: bytes):
    with ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_safe_round_trip_preserves_valid_png_chart_relationships_and_shared_strings(tmp_path):
    source_path, source = _prepare(tmp_path, dependent=False)
    plan = _make_plan(source, cell="G2", value=TypedValue("string", "confirmed"))
    candidate = XlsxAdapter().apply(source, plan)
    assert candidate is not None
    assert source_path.read_bytes() == source

    old_parts, new_parts = _package_parts(source), _package_parts(candidate)
    _validate_png(old_parts["xl/media/image1.png"])
    _validate_png(new_parts["xl/media/image1.png"])
    assert old_parts["xl/media/image1.png"] == new_parts["xl/media/image1.png"]
    assert old_parts["xl/sharedStrings.xml"] == new_parts["xl/sharedStrings.xml"]
    preserved_visual_parts = {
        name for name in old_parts
        if name.startswith(("xl/charts/", "xl/drawings/", "xl/media/"))
    }
    assert preserved_visual_parts
    assert all(old_parts[name] == new_parts[name] for name in preserved_visual_parts)
    report = IndependentVerifier().verify(source, candidate, plan)
    assert report.outcome == "VERIFIED"


def test_formula_dependent_round_trip_preserves_valid_png_and_remains_incomplete(tmp_path):
    source_path, source = _prepare(tmp_path, dependent=True)
    plan = _make_plan(source, cell="D2", value=TypedValue("decimal", 200.5))
    candidate = XlsxAdapter().apply(source, plan)
    assert candidate is not None
    assert source_path.read_bytes() == source

    old_parts, new_parts = _package_parts(source), _package_parts(candidate)
    _validate_png(old_parts["xl/media/image1.png"])
    _validate_png(new_parts["xl/media/image1.png"])
    assert old_parts["xl/media/image1.png"] == new_parts["xl/media/image1.png"]
    assert old_parts["xl/charts/chart1.xml"] == new_parts["xl/charts/chart1.xml"]
    report = IndependentVerifier().verify(source, candidate, plan)
    assert report.outcome == "INCOMPLETE"
    assert report.check("dependents").status == "NOT_VERIFIED"
