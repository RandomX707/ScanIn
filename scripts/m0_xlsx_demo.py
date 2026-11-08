#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from decimal import Decimal
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("m0_xlsx_demo")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.core.adapters.xlsx.adapter import XlsxAdapter, XlsxError
from packages.core.models.contracts import Patch, PatchPlan, TypedValue
from packages.core.planning.planner import PlanError, build_patch_plan
from packages.core.verification.independent import IndependentVerifier


def typed(data: dict[str, Any]) -> TypedValue:
    value = data.get("value")
    if data["type"] == "decimal" and isinstance(value, str):
        value = Decimal(value)
    return TypedValue(data["type"], value, data.get("native"), data.get("display"))


def patch_from(data: dict[str, Any]) -> Patch:
    return Patch(data["source_version_id"], data["target_id"], typed(data["expected_old_value"]),
                 data["expected_fingerprint"], typed(data["new_value"]))


def plan_dict(plan: PatchPlan) -> dict[str, Any]:
    return {
        "source_version_id": plan.source_version_id, "source_sha256": plan.source_sha256,
        "patches": [{"source_version_id": p.source_version_id, "target_id": p.target_id,
                     "expected_old_value": p.expected_old_value.canonical(),
                     "expected_fingerprint": p.expected_fingerprint, "new_value": p.new_value.canonical()} for p in plan.patches],
        "permitted_changes": [asdict(p) for p in plan.permitted_changes],
        "provenance": plan.provenance_dict(), "dependents": list(plan.dependents),
        "dependencies_unknown": plan.dependencies_unknown, "no_change": plan.no_change,
        "plan_sha256": plan.plan_sha256,
    }


def freeze_plan(adapter: XlsxAdapter, source: bytes, data: dict[str, Any]) -> PatchPlan:
    plan = build_patch_plan(adapter, source, [patch_from(p) for p in data.get("patches", [])], provenance=data["provenance"])
    if plan.plan_sha256 != data.get("plan_sha256") or plan_dict(plan) != data:
        raise PlanError("frozen_plan_mismatch")
    return plan


def emit(data: Any, path: str | None = None) -> None:
    rendered = json.dumps(data, sort_keys=True, indent=2, default=str) + "\n"
    if path:
        Path(path).write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


def inspect_cmd(args) -> None:
    LOG.info("Inspecting %s", args.source)
    profile = XlsxAdapter().inspect(Path(args.source).read_bytes())
    emit(asdict(profile), args.output)


def targets_cmd(args) -> None:
    adapter, source = XlsxAdapter(), Path(args.source).read_bytes()
    all_targets = adapter.targets(source) + adapter.unsupported_targets(source)
    emit([asdict(t) | {"value": t.value.canonical(), "dependents": list(t.dependents) if t.dependents is not None else None}
          for t in all_targets], args.output)


def plan_cmd(args) -> None:
    adapter, source = XlsxAdapter(), Path(args.source).read_bytes()
    patch_doc = json.loads(Path(args.patches).read_text(encoding="utf-8"))
    provenance = json.loads(Path(args.provenance).read_text(encoding="utf-8"))
    patches = [patch_from(p) for p in patch_doc]
    plan = build_patch_plan(adapter, source, patches, provenance=provenance)
    emit(plan_dict(plan), args.output)


def apply_cmd(args) -> None:
    adapter, source = XlsxAdapter(), Path(args.source).read_bytes()
    source_path, output_path = Path(args.source), Path(args.output)
    same_path = source_path.resolve() == output_path.resolve()
    same_file = output_path.exists() and os.path.samefile(source_path, output_path)
    if same_path or same_file:
        raise PlanError("output_must_be_new_copy")
    plan_data = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    plan = freeze_plan(adapter, source, plan_data)
    output = adapter.apply(source, plan)
    if output is None:
        emit({"status": "NO_CHANGE", "source_sha256": plan.source_sha256, "derived_file": None})
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(output)
    LOG.info("Wrote candidate copy %s", output_path)
    emit({"status": "CANDIDATE_CREATED", "output": args.output, "sha256": hashlib.sha256(output).hexdigest()})


def verify_cmd(args) -> None:
    adapter, source = XlsxAdapter(), Path(args.source).read_bytes()
    plan_data = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    plan = freeze_plan(adapter, source, plan_data)
    report = IndependentVerifier().verify(source, Path(args.candidate).read_bytes(), plan)
    emit(report.as_dict(), args.output)
    if report.outcome == "BLOCKED":
        raise SystemExit(2)


def demo_cmd(args) -> None:
    # The fixture factory lives with the synthetic corpus; it contains no real customer data.
    from tests.conftest import add_shared_strings, add_unsupported_cells, make_fixture

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    adapter = XlsxAdapter()
    success_source_path = outdir / "m0_original.xlsx"
    success_candidate_path = outdir / "m0_edited.xlsx"
    successful_report_path = outdir / "m0_verification.json"
    make_fixture(success_source_path, dependent=False, with_objects=True)
    add_shared_strings(success_source_path)
    add_unsupported_cells(success_source_path)
    source = success_source_path.read_bytes()
    safe = next(t for t in adapter.targets(source) if t.sheet == "Claim" and t.cell == "G2")
    patch = Patch(hashlib.sha256(source).hexdigest(), safe.id, safe.value, safe.fingerprint, TypedValue("string", "confirmed"))
    provenance = {"edit_set_id": "m0-success", "actor_id": "demo", "created_at": "2026-11-06T00:00:00Z"}
    (outdir / "m0_provenance.json").write_text(json.dumps(provenance, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (outdir / "m0_patches.json").write_text(json.dumps([{
        "source_version_id": patch.source_version_id, "target_id": patch.target_id,
        "expected_old_value": patch.expected_old_value.canonical(),
        "expected_fingerprint": patch.expected_fingerprint,
        "new_value": patch.new_value.canonical(),
    }], sort_keys=True, indent=2) + "\n", encoding="utf-8")
    plan = build_patch_plan(adapter, source, [patch], provenance=provenance)
    plan_path = outdir / "m0_patch_plan.json"
    plan_path.write_text(json.dumps(plan_dict(plan), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    candidate = adapter.apply(source, plan)
    assert candidate is not None
    success_candidate_path.write_bytes(candidate)
    report = IndependentVerifier().verify(source, candidate, plan)
    successful_report_path.write_text(json.dumps(report.as_dict(), sort_keys=True, indent=2) + "\n", encoding="utf-8")

    dep_source_path = outdir / "m0_dependent_original.xlsx"
    dep_candidate_path = outdir / "m0_dependent_edited.xlsx"
    dep_report_path = outdir / "m0_incomplete_verification.json"
    make_fixture(dep_source_path, dependent=True, with_objects=True)
    add_shared_strings(dep_source_path)
    add_unsupported_cells(dep_source_path)
    dep_source = dep_source_path.read_bytes()
    amount = next(t for t in adapter.targets(dep_source) if t.sheet == "Claim" and t.cell == "D2")
    dep_patch = Patch(hashlib.sha256(dep_source).hexdigest(), amount.id, amount.value, amount.fingerprint, TypedValue("decimal", 200.5))
    dep_plan = build_patch_plan(adapter, dep_source, [dep_patch], provenance={"edit_set_id": "m0-dependent", "actor_id": "demo", "created_at": "2026-11-06T00:00:00Z"})
    dep_plan_path = outdir / "m0_dependent_patch_plan.json"
    dep_plan_path.write_text(json.dumps(plan_dict(dep_plan), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    dep_candidate = adapter.apply(dep_source, dep_plan)
    assert dep_candidate is not None
    dep_candidate_path.write_bytes(dep_candidate)
    dep_report = IndependentVerifier().verify(dep_source, dep_candidate, dep_plan)
    dep_report_path.write_text(json.dumps(dep_report.as_dict(), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    emit({"successful": {"source": str(success_source_path), "candidate": str(success_candidate_path), "outcome": report.outcome},
          "dependent": {"source": str(dep_source_path), "candidate": str(dep_candidate_path), "outcome": dep_report.outcome},
          "reports": [str(successful_report_path), str(dep_report_path)]})


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="M0 deterministic XLSX inspect, plan, apply and verify proof spike")
    sub = root.add_subparsers(dest="command", required=True)
    p = sub.add_parser("inspect"); p.add_argument("source"); p.add_argument("--output"); p.set_defaults(func=inspect_cmd)
    p = sub.add_parser("targets"); p.add_argument("source"); p.add_argument("--output"); p.set_defaults(func=targets_cmd)
    p = sub.add_parser("plan"); p.add_argument("source"); p.add_argument("--patches", required=True); p.add_argument("--provenance", required=True); p.add_argument("--output", required=True); p.set_defaults(func=plan_cmd)
    p = sub.add_parser("apply"); p.add_argument("source"); p.add_argument("--plan", required=True); p.add_argument("--output", required=True); p.set_defaults(func=apply_cmd)
    p = sub.add_parser("verify"); p.add_argument("source"); p.add_argument("candidate"); p.add_argument("--plan", required=True); p.add_argument("--output"); p.set_defaults(func=verify_cmd)
    p = sub.add_parser("demo"); p.add_argument("--outdir", default=str(ROOT / "artifacts" / "m0")); p.set_defaults(func=demo_cmd)
    return root


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = parser().parse_args()
    try:
        args.func(args)
    except (PlanError, XlsxError, ValueError, OSError, KeyError) as exc:
        print(json.dumps({"error": getattr(exc, "code", "operation_failed"), "message": str(exc)}, sort_keys=True), file=sys.stderr)
        raise SystemExit(1) from exc
