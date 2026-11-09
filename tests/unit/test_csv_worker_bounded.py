"""Phase 2B: one restricted-worker request parses a CSV once and never builds the full target list."""
from __future__ import annotations

import hashlib

import pytest

from packages.core.adapters.csv_adapter.adapter import CsvAdapter
from packages.core.models.contracts import Patch, TypedValue
from packages.core.planning.planner import PlanError, build_patch_plan
from packages.workers.parse_entry import MAX_TARGETS, _target_dict, inspect_bytes


def _source(rows: int = 700) -> bytes:
    lines = [b"claim_number,insured_name,amount,note"]
    lines += [f'CLM-{i:05d},"Rao, {i}",{i}.25,"said ""ok"" {i}"'.encode() for i in range(1, rows + 1)]
    return b"\r\n".join(lines) + b"\r\n"


@pytest.fixture
def counted(monkeypatch):
    calls = {"scan": 0}
    original = CsvAdapter._scan

    def scan(self, source):
        calls["scan"] += 1
        return original(self, source)

    def forbidden(self, source):
        raise AssertionError("full CSV target list must not be built by the worker")

    monkeypatch.setattr(CsvAdapter, "_scan", scan)
    monkeypatch.setattr(CsvAdapter, "targets", forbidden)
    monkeypatch.setattr(CsvAdapter, "unsupported_targets", forbidden)
    return calls


def test_inspection_parses_once_and_pages_without_full_target_list(counted):
    source = _source()
    result = inspect_bytes(source, filename="claims.csv", include_targets=True, include_summary_evidence=True,
                           target_offset=MAX_TARGETS)
    assert counted["scan"] == 1
    assert result["targets_page"] == {"mode": "page", "offset": 2000, "limit": 2000, "total": 2804, "next_offset": None}
    assert [t["id"] for t in result["targets"]][:2] == ["csv:501:1", "csv:501:2"] and len(result["targets"]) == 804
    assert result["structural_summary"]["statistics"] == {"record_count": 700, "column_count": 4}
    assert result["summary_evidence"][0]["id"] == "csv:header"


def test_exact_lookup_parses_once_and_matches_full_list(counted, monkeypatch):
    source = _source()
    result = inspect_bytes(source, filename="claims.csv", include_targets=True,
                           target_ids=["csv:700:4", "csv:2:2", "csv:999:1"])
    assert counted["scan"] == 1
    monkeypatch.undo()
    full = {t.id: _target_dict(t) for t in CsvAdapter().targets(source)}
    assert result["targets"] == [full["csv:2:2"], full["csv:700:4"]]
    assert result["targets_page"]["found"] == 2 and result["targets_page"]["total"] == len(full)
    assert result["targets"][1]["fingerprint"] == hashlib.sha256(b'"said ""ok"" 699"').hexdigest()


def test_pages_equal_the_legacy_full_list_slices():
    source = _source(1100)
    full = [_target_dict(t) for t in CsvAdapter().targets(source)]
    pages, offset = [], 0
    while offset is not None:
        result = inspect_bytes(source, filename="claims.csv", include_targets=True, target_offset=offset)
        pages.extend(result["targets"])
        offset = result["targets_page"]["next_offset"]
    assert pages == full


def test_mixed_line_endings_page_contains_read_only_targets():
    source = b"a,b\r\nc,d\ne,f\r\n"
    result = inspect_bytes(source, filename="mixed.csv", include_targets=True)
    assert result["capability"]["level"] == "Read-only"
    assert [(t["id"], t["editable"], t["reason"]) for t in result["targets"]][:2] == [
        ("csv:1:1", False, "mixed_line_endings"), ("csv:1:2", False, "mixed_line_endings")]


def test_planner_resolves_only_patched_targets_and_keeps_every_safety_check(counted):
    source = _source()
    adapter = CsvAdapter()
    target = adapter.targets_by_id(source, ["csv:690:2"])[0]
    good = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint,
                 TypedValue("string", "Rao, edited"))
    plan = build_patch_plan(adapter, source, (good,), provenance={"edit_set_id": "e", "actor_id": "owner"})
    assert [p.target_id for p in plan.patches] == ["csv:690:2"] and counted["scan"] == 1
    for bad, code in [
        (Patch(good.source_version_id, target.id, target.value, "0" * 64, good.new_value), "fingerprint_mismatch"),
        (Patch(good.source_version_id, target.id, TypedValue("string", "other"), target.fingerprint, good.new_value),
         "expected_value_mismatch"),
        (Patch(good.source_version_id, "csv:9999:1", target.value, target.fingerprint, good.new_value), "target_not_found"),
        (Patch(good.source_version_id, target.id, target.value, target.fingerprint, TypedValue("string", "=1+1")),
         "unsafe_csv_value"),
    ]:
        with pytest.raises(PlanError) as error:
            build_patch_plan(adapter, source, (bad,), provenance={"edit_set_id": "e", "actor_id": "owner"})
        assert error.value.code == code


def test_mixed_line_endings_patch_is_refused_by_the_planner():
    source = b"a,b\r\nc,d\ne,f\r\n"
    adapter = CsvAdapter()
    target = adapter.targets_by_id(source, ["csv:2:1"])[0]
    patch = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint,
                  TypedValue("string", "x"))
    with pytest.raises(PlanError) as error:
        build_patch_plan(adapter, source, (patch,), provenance={"edit_set_id": "e", "actor_id": "owner"})
    assert error.value.code == "mixed_line_endings"


@pytest.mark.parametrize("source", [b"claim,name\nC1,x\x81y\n", b"\xef\xbb\xbfclaim,name\nC1,\xff\n"])
def test_unparsable_read_only_csv_inspects_without_targets_instead_of_failing(source):
    for kwargs in ({}, {"target_offset": 0}, {"target_ids": ["csv:2:2"]}):
        result = inspect_bytes(source, filename="claims.csv", include_targets=True, include_summary_evidence=True, **kwargs)
        assert result["status"] == "inspected"
        assert result["capability"] == {"level": "Read-only", "reasons": ["unsupported_encoding"]}
        assert result["targets"] == [] and result["targets_page"]["total"] == 0 and result["targets_truncated"] is False
        assert result["structural_summary"]["statistics"] == {}
        assert "could not be read reliably" in result["structural_summary"]["description"]
        assert "summary_evidence" not in result or result["summary_evidence"] == []


@pytest.mark.parametrize("limit_name,source,reason", [
    ("MAX_CSV_CELLS", b"a,b,c\n1,2,3\n", "csv_cell_limit_exceeded"),
    ("MAX_CSV_ROWS", b"a\nb\nc\nd\n", "csv_row_limit_exceeded"),
])
def test_resource_limits_are_rejected_with_their_stable_code(monkeypatch, limit_name, source, reason):
    import packages.core.adapters.csv_adapter.adapter as module
    from packages.core.adapters.registry import AdapterRegistry
    from packages.workers.parse_entry import ParserRejected
    monkeypatch.setattr(module, limit_name, 3)
    selection = AdapterRegistry().inspect(source, filename="big.csv")
    assert (selection.profile.level, selection.profile.reasons, selection.media_type) == ("rejected", (reason,), "text/csv")
    with pytest.raises(ParserRejected) as error:
        inspect_bytes(source, filename="big.csv", include_targets=True)
    assert error.value.reason_code == reason and error.value.digest == hashlib.sha256(source).hexdigest()


def test_malformed_csv_still_fails_closed_as_unsupported_text():
    from packages.core.adapters.registry import AdapterRegistry
    selection = AdapterRegistry().inspect(b'claim,name\nC1,"open\n', filename="bad.csv")
    assert (selection.profile.level, selection.profile.reasons) == ("read_only", ("adapter_not_implemented",))
