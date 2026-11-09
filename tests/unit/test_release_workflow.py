from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from packages.core.adapters.xlsx.adapter import XlsxAdapter
from packages.core.models.contracts import Patch, TypedValue
from packages.core.planning.planner import PlanError
from packages.workflow.release import (
    DataCorrection,
    WorkflowRejected,
    XlsxReleaseWorkflow,
    export_corrections,
)
from packages.registry.documents import is_download_eligible
from tests.conftest import make_fixture


def _patch(source: bytes, cell: str, new_value: TypedValue) -> Patch:
    target = next(t for t in XlsxAdapter().targets(source) if t.cell == cell)
    return Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint, new_value)


def _freeze(source: bytes, patch: Patch):
    return XlsxReleaseWorkflow().freeze(
        source=source,
        document_id="doc-1",
        base_version_id=hashlib.sha256(source).hexdigest(),
        actor_id="reviewer-1",
        role="OWNER",
        authorized_category="insurer-authored",
        reason="Correct the approved policy limit",
        patches=(patch,),
    )


def test_verified_xlsx_release_exposes_only_independently_verified_bytes(tmp_path):
    source = make_fixture(tmp_path / "source.xlsx").read_bytes()
    original = bytes(source)
    patch = _patch(source, "G2", TypedValue("string", "approved value"))
    frozen = XlsxReleaseWorkflow().freeze(
        source=source, document_id="doc-1", base_version_id="version-uuid-1", actor_id="reviewer-1",
        role="OWNER", authorized_category="insurer-authored", reason="Correct the approved policy limit",
        patches=(patch,),
    )
    result = XlsxReleaseWorkflow().release(source=source, edit_set=frozen)
    assert source == original
    assert result.status == "VERIFIED"
    assert result.derived_bytes is not None
    assert result.derived_sha256 == hashlib.sha256(result.derived_bytes).hexdigest()
    assert result.verification is not None and result.verification.outcome == "VERIFIED"


def test_formula_dependent_release_is_incomplete_and_withheld_by_default(tmp_path):
    source = make_fixture(tmp_path / "source.xlsx").read_bytes()
    frozen = _freeze(source, _patch(source, "D2", TypedValue("integer", 125)))
    result = XlsxReleaseWorkflow().release(source=source, edit_set=frozen)
    assert result.status == "INCOMPLETE"
    assert result.verification is not None
    assert result.derived_bytes is None
    assert result.derived_sha256 is None


def test_noop_produces_no_candidate(tmp_path):
    source = make_fixture(tmp_path / "source.xlsx").read_bytes()
    current = next(t for t in XlsxAdapter().targets(source) if t.cell == "G2")
    frozen = _freeze(source, _patch(source, "G2", current.value))
    result = XlsxReleaseWorkflow().release(source=source, edit_set=frozen)
    assert result.status == "NO_CHANGE"
    assert result.derived_bytes is None


def test_category_policy_denies_third_party_and_unknown_documents(tmp_path):
    source = make_fixture(tmp_path / "source.xlsx").read_bytes()
    workflow = XlsxReleaseWorkflow()
    with pytest.raises(WorkflowRejected, match="authorized_insurer_document"):
        workflow.freeze(source=source, document_id="doc", base_version_id=hashlib.sha256(source).hexdigest(),
                        actor_id="processor", role="OWNER", authorized_category="third-party-issued",
                        reason="Correction", patches=(_patch(source, "G2", TypedValue("string", "x")),))


def test_formula_cell_patch_is_rejected_during_preflight(tmp_path):
    source = make_fixture(tmp_path / "source.xlsx").read_bytes()
    target = next(t for t in XlsxAdapter().unsupported_targets(source) if t.cell == "H2")
    patch = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value,
                  target.fingerprint, TypedValue("integer", 10))
    with pytest.raises(PlanError, match="formula_cell"):
        _freeze(source, patch)


def test_frozen_edit_set_corruption_is_blocked(tmp_path):
    source = make_fixture(tmp_path / "source.xlsx").read_bytes()
    frozen = _freeze(source, _patch(source, "G2", TypedValue("string", "approved value")))
    altered_plan = replace(frozen.plan, source_sha256="0" * 64)
    tampered = replace(frozen, plan=altered_plan)
    result = XlsxReleaseWorkflow().release(source=source, edit_set=tampered)
    assert result.status == "BLOCKED"
    assert result.derived_bytes is None


@pytest.mark.parametrize(("outcome", "adapter", "manifest", "expected"), [
    ("VERIFIED", "csv", {}, True),
    ("STRUCTURALLY_VERIFIED", "docx", {
        "verification_profile": "structural", "structural_checks": "PASS",
        "visual_checks": "NOT_PERFORMED",
    }, True),
    ("STRUCTURALLY_VERIFIED", "pdf", {
        "verification_profile": "structural", "structural_checks": "PASS",
        "visual_checks": "NOT_PERFORMED",
    }, False),
    ("STRUCTURALLY_VERIFIED", "docx", {
        "verification_profile": "structural_and_visual", "structural_checks": "PASS",
        "visual_checks": "NOT_PERFORMED",
    }, False),
    ("STRUCTURALLY_VERIFIED", "docx", {
        "verification_profile": "structural", "structural_checks": "FAIL",
        "visual_checks": "NOT_PERFORMED",
    }, False),
    ("STRUCTURALLY_VERIFIED", "docx", {
        "verification_profile": "structural", "structural_checks": "PASS",
        "visual_checks": "PASS",
    }, False),
    ("INCOMPLETE", "xlsx", {}, False),
    ("BLOCKED", "docx", {}, False),
])
def test_download_eligibility_uses_release_outcome_adapter_and_manifest(outcome, adapter, manifest, expected):
    assert is_download_eligible(outcome, adapter, manifest) is expected


def test_frozen_edit_set_metadata_is_bound_to_the_freeze_hash(tmp_path):
    source = make_fixture(tmp_path / "source.xlsx").read_bytes()
    frozen = _freeze(source, _patch(source, "G2", TypedValue("string", "approved value")))
    tampered = replace(frozen, reason="Different reason")
    result = XlsxReleaseWorkflow().release(source=source, edit_set=tampered)
    assert result.status == "BLOCKED"
    assert result.derived_bytes is None


def test_data_correction_sidecar_does_not_write_document_bytes(tmp_path):
    source = make_fixture(tmp_path / "source.xlsx").read_bytes()
    original = bytes(source)
    correction = DataCorrection("field-1", "version-1", TypedValue("decimal", 123.5),
                                "Corrected from the signed schedule", "reviewer-1")
    exported = export_corrections([correction])
    payload = json.loads(exported)
    assert payload[0]["field_id"] == "field-1"
    assert payload[0]["created_at"].endswith("+00:00")
    assert source == original
