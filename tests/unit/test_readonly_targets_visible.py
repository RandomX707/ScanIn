"""B-1: every safely parsed value stays viewable once, with its real editable flag and reason."""
from __future__ import annotations

import hashlib

import pytest

from packages.core.adapters.xlsx.adapter import XlsxAdapter
from packages.core.models.contracts import Patch, TypedValue
from packages.core.planning.planner import PlanError, build_patch_plan
from packages.workers.parse_entry import inspect_bytes
from scripts.host.create_browser_fixtures import create_readonly_values_xlsx, create_tracked_change_docx

EXPECTED = {
    "xlsx:Claim!B2": (True, None, "Meera Iyer"),
    "xlsx:Locked!A2": (True, None, "Unlocked adjuster note"),
    "xlsx:Locked!A1": (False, "protected_cell_locked", "Locked reserve amount"),
    "xlsx:Claim!B6": (False, "unsupported_data_validation", "Pending"),
    "xlsx:Claim!B4": (False, "rich_text", "Urgent: tow required"),
    "xlsx:Claim!B5": (False, "time_value_read_only", "15:30:00"),
    "xlsx:Claim!B3": (False, "formula_cell", "=LEN(B2)"),
}


@pytest.fixture(scope="module")
def source() -> bytes:
    return create_readonly_values_xlsx()


def test_every_value_appears_exactly_once_with_its_real_flag_and_reason(source):
    result = inspect_bytes(source, filename="readonly-values.xlsx", include_targets=True)
    ids = [t["id"] for t in result["targets"]]
    assert len(ids) == len(set(ids)) == result["targets_page"]["total"]
    by_id = {t["id"]: t for t in result["targets"]}
    for target_id, (editable, reason, value) in EXPECTED.items():
        target = by_id[target_id]
        assert (target["editable"], target["reason"], target["value"]["value"]) == (editable, reason, value), target_id
    assert result["capability"]["level"] == "editable"
    assert result["targets_editable_total"] == sum(1 for t in result["targets"] if t["editable"])


def test_exact_lookup_and_pages_agree_with_fingerprints(source):
    page = {t["id"]: t for t in inspect_bytes(source, filename="r.xlsx", include_targets=True)["targets"]}
    lookup = inspect_bytes(source, filename="r.xlsx", include_targets=True, target_ids=list(EXPECTED) + ["xlsx:Locked!Z9"])
    assert lookup["targets_page"]["found"] == len(EXPECTED) and lookup["targets_page"]["total"] == len(page)
    for target in lookup["targets"]:
        assert target == page[target["id"]]
    assert page["xlsx:Locked!A1"]["fingerprint"] == next(
        t.fingerprint for t in XlsxAdapter().targets(source) if t.id == "xlsx:Locked!A1")


@pytest.mark.parametrize("target_id,code", [
    ("xlsx:Locked!A1", "protected_cell_locked"),
    ("xlsx:Claim!B6", "unsupported_data_validation"),
    ("xlsx:Claim!B4", "rich_text"),
    ("xlsx:Claim!B5", "time_value_read_only"),
    ("xlsx:Claim!B3", "formula_cell"),
])
def test_read_only_values_are_still_refused_for_native_patching(source, target_id, code):
    adapter = XlsxAdapter()
    target = next(t for t in adapter.targets(source) + adapter.unsupported_targets(source) if t.id == target_id)
    patch = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint, TypedValue("string", "changed"))
    with pytest.raises(PlanError) as error:
        build_patch_plan(adapter, source, (patch,), provenance={"edit_set_id": "b1", "actor_id": "owner"})
    assert error.value.code == code


def test_editable_cells_on_the_same_workbook_still_patch(source):
    adapter = XlsxAdapter()
    for target_id in ("xlsx:Claim!B2", "xlsx:Locked!A2"):
        target = next(t for t in adapter.targets(source) if t.id == target_id)
        plan = build_patch_plan(adapter, source, (Patch(hashlib.sha256(source).hexdigest(), target.id, target.value,
                                                        target.fingerprint, TypedValue("string", "Edited")),),
                                provenance={"edit_set_id": "b1", "actor_id": "owner"})
        assert [p.target_id for p in plan.patches] == [target_id]


def test_documents_whose_read_only_targets_are_also_unsupported_targets_have_no_duplicates():
    result = inspect_bytes(create_tracked_change_docx(), filename="tracked.docx", include_targets=True)
    ids = [t["id"] for t in result["targets"]]
    assert ids and len(ids) == len(set(ids)) and all(not t["editable"] for t in result["targets"])
