from __future__ import annotations

from decimal import Decimal
from typing import Any

from packages.core.models.contracts import ChangePermission, Patch, PatchPlan, TypedValue
from packages.core.planning.planner import calculate_plan_sha256
from packages.workflow.release import FrozenEditSet, _edit_set_digest


def _typed_dict(value: TypedValue) -> dict[str, Any]:
    return value.canonical()


def _typed_value(value: dict[str, Any]) -> TypedValue:
    kind = value.get("type")
    item = value.get("value")
    if kind == "decimal":
        item = Decimal(str(item))
    return TypedValue(kind, item, native=value.get("native"), display=value.get("display"))


def serialize_frozen_edit_set(edit_set: FrozenEditSet) -> dict[str, Any]:
    plan = edit_set.plan
    return {
        "edit_set_id": edit_set.edit_set_id,
        "document_id": edit_set.document_id,
        "base_version_id": edit_set.base_version_id,
        "actor_id": edit_set.actor_id,
        "reason": edit_set.reason,
        "authorized_category": edit_set.authorized_category,
        "frozen_sha256": edit_set.frozen_sha256,
        "plan": {
            "source_version_id": plan.source_version_id,
            "source_sha256": plan.source_sha256,
            "patches": [{
                "source_version_id": patch.source_version_id,
                "target_id": patch.target_id,
                "expected_old_value": _typed_dict(patch.expected_old_value),
                "expected_fingerprint": patch.expected_fingerprint,
                "new_value": _typed_dict(patch.new_value),
            } for patch in plan.patches],
            "permitted_changes": [{
                "part": permission.part,
                "locator": permission.locator,
                "operation": permission.operation,
                "old_fingerprint": permission.old_fingerprint,
                "expected_new_fingerprint": permission.expected_new_fingerprint,
            } for permission in plan.permitted_changes],
            "provenance": dict(plan.provenance),
            "dependents": list(plan.dependents),
            "dependencies_unknown": plan.dependencies_unknown,
            "no_change": plan.no_change,
            "plan_sha256": plan.plan_sha256,
        },
    }


def deserialize_frozen_edit_set(payload: dict[str, Any]) -> FrozenEditSet:
    try:
        plan_data = payload["plan"]
        patches = tuple(Patch(
            source_version_id=item["source_version_id"],
            target_id=item["target_id"],
            expected_old_value=_typed_value(item["expected_old_value"]),
            expected_fingerprint=item["expected_fingerprint"],
            new_value=_typed_value(item["new_value"]),
        ) for item in plan_data["patches"])
        permissions = tuple(ChangePermission(**item) for item in plan_data["permitted_changes"])
        plan = PatchPlan(
            source_version_id=plan_data["source_version_id"],
            source_sha256=plan_data["source_sha256"],
            patches=patches,
            permitted_changes=permissions,
            provenance=tuple(sorted(plan_data["provenance"].items())),
            dependents=tuple(plan_data["dependents"]),
            dependencies_unknown=bool(plan_data["dependencies_unknown"]),
            no_change=bool(plan_data["no_change"]),
            plan_sha256=plan_data["plan_sha256"],
        )
        frozen = FrozenEditSet(
            edit_set_id=payload["edit_set_id"], document_id=payload["document_id"],
            base_version_id=payload["base_version_id"], actor_id=payload["actor_id"],
            reason=payload["reason"], authorized_category=payload["authorized_category"],
            plan=plan, frozen_sha256=payload["frozen_sha256"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("frozen_plan_payload_invalid") from error

    if calculate_plan_sha256(frozen.plan) != frozen.plan.plan_sha256:
        raise ValueError("frozen_plan_integrity_failed")
    digest = _edit_set_digest(
        edit_set_id=frozen.edit_set_id, document_id=frozen.document_id,
        base_version_id=frozen.base_version_id, actor_id=frozen.actor_id,
        reason=frozen.reason, authorized_category=frozen.authorized_category,
        plan_sha256=frozen.plan.plan_sha256,
    )
    if digest != frozen.frozen_sha256:
        raise ValueError("frozen_edit_set_integrity_failed")
    return frozen
