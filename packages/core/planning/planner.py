from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from packages.core.models.contracts import ChangePermission, Patch, PatchPlan


class PlanError(ValueError):
    def __init__(self, code: str, target_id: str | None = None):
        self.code = code
        self.target_id = target_id
        super().__init__(f"{code}" + (f":{target_id}" if target_id else ""))


def calculate_plan_sha256(plan: PatchPlan) -> str:
    if not plan.patches:
        normalized = {"source_sha256": plan.source_sha256, "patches": [], "provenance": plan.provenance}
    else:
        normalized = {
            "source_sha256": plan.source_sha256,
            "patches": [
                {"source_version_id": p.source_version_id, "target_id": p.target_id,
                 "expected_old_value": p.expected_old_value.canonical(),
                 "expected_fingerprint": p.expected_fingerprint, "new_value": p.new_value.canonical()}
                for p in plan.patches
            ],
            "permissions": [asdict(p) for p in plan.permitted_changes],
            "provenance": plan.provenance,
            "dependents": list(plan.dependents),
            "dependencies_unknown": plan.dependencies_unknown,
            "no_change": plan.no_change,
        }
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_patch_plan(adapter, source: bytes, patches: list[Patch] | tuple[Patch, ...], *, provenance: dict[str, str]) -> PatchPlan:
    source_hash = hashlib.sha256(source).hexdigest()
    if not patches:
        frozen_provenance = tuple(sorted((str(k), str(v)) for k, v in provenance.items()))
        normalized = {"source_sha256": source_hash, "patches": [], "provenance": frozen_provenance}
        plan_hash = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return PatchPlan(source_hash, source_hash, (), (), frozen_provenance, (), False, True, plan_hash)
    versions = {p.source_version_id for p in patches}
    if len(versions) != 1 or next(iter(versions)) != source_hash:
        raise PlanError("stale_source")
    targets = {t.id: t for t in adapter.targets(source)}
    unsupported = {t.id: t for t in adapter.unsupported_targets(source)}
    seen: set[str] = set()
    dependencies: set[str] = set()
    unknown = False
    no_change = True
    permissions: list[ChangePermission] = []
    for patch in patches:
        if patch.target_id in seen:
            raise PlanError("duplicate_target", patch.target_id)
        seen.add(patch.target_id)
        target = targets.get(patch.target_id)
        if target is None:
            reason = unsupported.get(patch.target_id)
            raise PlanError(reason.reason if reason else "target_not_found", patch.target_id)
        if not target.editable:
            raise PlanError(target.reason or "not_editable", patch.target_id)
        if target.fingerprint != patch.expected_fingerprint:
            raise PlanError("fingerprint_mismatch", patch.target_id)
        if target.value.type != patch.expected_old_value.type or target.value.value != patch.expected_old_value.value:
            raise PlanError("expected_value_mismatch", patch.target_id)
        if patch.source_version_id != source_hash:
            raise PlanError("stale_source", patch.target_id)
        try:
            adapter.validate_new_value(patch.new_value)
        except Exception as error:
            code = getattr(error, "code", None)
            if code:
                raise PlanError(code, patch.target_id) from None
            raise
        compatible = getattr(adapter, "type_compatible", None)
        allowed = compatible(target.value, patch.new_value) if compatible is not None else (
            target.value.type == patch.new_value.type
            or {target.value.type, patch.new_value.type} <= {"integer", "decimal"}
        )
        if not allowed:
            raise PlanError("type_mismatch", patch.target_id)
        same = patch.new_value.type == target.value.type and patch.new_value.value == target.value.value
        no_change &= same
        if not same:
            known, is_unknown = adapter.formula_dependents(source, target)
            dependencies.update(known)
            unknown |= is_unknown
            try:
                perm = adapter.permission_for(source, target, patch.new_value, provenance)
            except Exception as error:
                code = getattr(error, "code", None)
                if code:
                    raise PlanError(code, patch.target_id) from None
                raise
            permissions.extend(perm)
    unique_permissions = {(p.part, p.locator, p.operation): p for p in permissions}
    permissions = list(unique_permissions.values())
    frozen_provenance = tuple(sorted((str(k), str(v)) for k, v in provenance.items()))
    normalized = {
        "source_sha256": source_hash,
        "patches": [
            {"source_version_id": p.source_version_id, "target_id": p.target_id,
             "expected_old_value": p.expected_old_value.canonical(),
             "expected_fingerprint": p.expected_fingerprint, "new_value": p.new_value.canonical()}
            for p in patches
        ],
        "permissions": [asdict(p) for p in permissions],
        "provenance": frozen_provenance,
        "dependents": sorted(dependencies), "dependencies_unknown": unknown, "no_change": no_change,
    }
    plan = PatchPlan(next(iter(versions)), source_hash, tuple(patches), tuple(permissions), frozen_provenance,
                     tuple(sorted(dependencies)), unknown, no_change, "")
    return PatchPlan(plan.source_version_id, plan.source_sha256, plan.patches, plan.permitted_changes, plan.provenance,
                     plan.dependents, plan.dependencies_unknown, plan.no_change, calculate_plan_sha256(plan))
