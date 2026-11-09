from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from packages.core.adapters.xlsx.adapter import XlsxAdapter
from packages.core.models.contracts import Patch, PatchPlan, TypedValue, VerifyReport
from packages.core.planning.planner import PlanError, build_patch_plan, calculate_plan_sha256
from packages.core.verification.independent import IndependentVerifier

Role = Literal["OWNER"]
Category = Literal["insurer-authored", "customer-form", "third-party-issued", "signed-legal", "unknown"]


def _edit_set_digest(*, edit_set_id: str, document_id: str, base_version_id: str, actor_id: str,
                     reason: str, authorized_category: Category, plan_sha256: str) -> str:
    body = {
        "edit_set_id": edit_set_id,
        "document_id": document_id,
        "base_version_id": base_version_id,
        "actor_id": actor_id,
        "reason": reason,
        "authorized_category": authorized_category,
        "plan_sha256": plan_sha256,
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class WorkflowRejected(ValueError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class FrozenEditSet:
    edit_set_id: str
    document_id: str
    base_version_id: str
    actor_id: str
    reason: str
    authorized_category: Category
    plan: PatchPlan
    frozen_sha256: str


@dataclass(frozen=True)
class ReleaseResult:
    status: Literal["NO_CHANGE", "VERIFIED", "STRUCTURALLY_VERIFIED", "INCOMPLETE", "BLOCKED"]
    edit_set_id: str
    verification: VerifyReport | None
    derived_bytes: bytes | None
    derived_sha256: str | None


class NativeReleaseWorkflow:
    """M2 policy/preflight/apply/independent-verification core for native files.

    Persistence, owner-authorized release audit transactions and derived-object
    commit are supplied by the API/database integration layer. This pure service
    never mutates its input and exposes bytes only when tenant policy explicitly
    permits an incomplete derivative; its default allows VERIFIED only.
    """

    def __init__(self, adapter: Any, verifier: Any):
        self.adapter = adapter
        self.verifier = verifier

    def freeze(self, *, source: bytes, document_id: str, base_version_id: str, actor_id: str,
               role: Role, authorized_category: Category, reason: str, patches: tuple[Patch, ...] | list[Patch],
               edit_set_id: str | None = None) -> FrozenEditSet:
        if role != "OWNER":
            raise WorkflowRejected("role_not_permitted")
        if not document_id or not base_version_id or not actor_id:
            raise WorkflowRejected("edit_set_identity_required")
        if authorized_category != "insurer-authored":
            raise WorkflowRejected("document_correction_requires_authorized_insurer_document")
        if not reason.strip() or len(reason) > 2000:
            raise WorkflowRejected("correction_reason_required")
        source_sha = hashlib.sha256(source).hexdigest()
        if self.adapter.inspect(source).level != "editable":
            raise WorkflowRejected("document_not_editable")
        edit_set_id = edit_set_id or str(uuid4())
        provenance = {
            "edit_set_id": edit_set_id,
            "document_id": document_id,
            "base_version_id": base_version_id,
            "actor_id": actor_id,
        }
        plan = build_patch_plan(self.adapter, source, patches, provenance=provenance)
        if calculate_plan_sha256(plan) != plan.plan_sha256:
            raise WorkflowRejected("patch_plan_integrity_failed")
        frozen_sha = _edit_set_digest(
            edit_set_id=edit_set_id,
            document_id=document_id,
            base_version_id=base_version_id,
            actor_id=actor_id,
            reason=reason.strip(),
            authorized_category=authorized_category,
            plan_sha256=plan.plan_sha256,
        )
        return FrozenEditSet(
            edit_set_id=edit_set_id,
            document_id=document_id,
            base_version_id=base_version_id,
            actor_id=actor_id,
            reason=reason.strip(),
            authorized_category=authorized_category,
            plan=plan,
            frozen_sha256=frozen_sha,
        )

    def release(self, *, source: bytes, edit_set: FrozenEditSet,
                allow_incomplete: bool = False) -> ReleaseResult:
        current_digest = _edit_set_digest(
            edit_set_id=edit_set.edit_set_id,
            document_id=edit_set.document_id,
            base_version_id=edit_set.base_version_id,
            actor_id=edit_set.actor_id,
            reason=edit_set.reason,
            authorized_category=edit_set.authorized_category,
            plan_sha256=edit_set.plan.plan_sha256,
        )
        if calculate_plan_sha256(edit_set.plan) != edit_set.plan.plan_sha256 or current_digest != edit_set.frozen_sha256:
            return ReleaseResult("BLOCKED", edit_set.edit_set_id, None, None, None)
        if edit_set.plan.no_change:
            return ReleaseResult("NO_CHANGE", edit_set.edit_set_id, None, None, None)
        try:
            candidate = self.adapter.apply(source, edit_set.plan)
        except Exception:
            return ReleaseResult("BLOCKED", edit_set.edit_set_id, None, None, None)
        if candidate is None:
            return ReleaseResult("BLOCKED", edit_set.edit_set_id, None, None, None)
        report = self.verifier.verify(source, candidate, edit_set.plan)
        if report.outcome in {"VERIFIED", "STRUCTURALLY_VERIFIED"} or (report.outcome == "INCOMPLETE" and allow_incomplete):
            return ReleaseResult(report.outcome, edit_set.edit_set_id, report, candidate,
                                 hashlib.sha256(candidate).hexdigest())
        return ReleaseResult(report.outcome, edit_set.edit_set_id, report, None, None)


class XlsxReleaseWorkflow(NativeReleaseWorkflow):
    """Default M0 XLSX implementation retained for existing callers."""

    def __init__(self, adapter: XlsxAdapter | None = None, verifier: IndependentVerifier | None = None):
        super().__init__(adapter or XlsxAdapter(), verifier or IndependentVerifier())


@dataclass(frozen=True)
class DataCorrection:
    field_id: str
    source_version_id: str
    corrected_value: TypedValue
    reason: str
    actor_id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.field_id or not self.source_version_id or not self.actor_id:
            raise ValueError("correction_identity_required")
        if not self.reason.strip() or len(self.reason) > 2000:
            raise ValueError("correction_reason_required")
        if self.created_at.tzinfo is None:
            raise ValueError("correction_timestamp_must_be_timezone_aware")


def export_corrections(corrections: tuple[DataCorrection, ...] | list[DataCorrection]) -> bytes:
    """Export value-only corrections; source files never enter this function."""
    payload = [{
        "field_id": item.field_id,
        "source_version_id": item.source_version_id,
        "corrected_value": item.corrected_value.canonical(),
        "reason": item.reason,
        "actor_id": item.actor_id,
        "created_at": item.created_at.isoformat(),
    } for item in corrections]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
