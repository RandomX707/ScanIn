from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import PurePosixPath
from typing import Any

from openpyxl import load_workbook

from packages.core.models.contracts import CheckResult, PatchPlan, VerifyReport

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
CUSTOM_NS = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
CUSTOM_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties"
PROVENANCE_NAME = "IDW_M0_Provenance"
CELL_ADDRESS = re.compile(r"^\$?([A-Z]{1,3})\$?([1-9][0-9]*)$", re.I)


class IndependentVerifier:
    """Read-only verifier with parsing and integrity checks separate from the XLSX writer."""

    def verify(self, source: bytes, candidate: bytes, plan: PatchPlan) -> VerifyReport:
        source_sha = hashlib.sha256(source).hexdigest()
        output_sha = hashlib.sha256(candidate).hexdigest()
        if _plan_digest(plan) != plan.plan_sha256:
            return self._blocked(source_sha, output_sha, "policy", "frozen_plan_integrity_failed")
        if source_sha != plan.source_sha256 or plan.source_version_id != source_sha:
            return self._blocked(source_sha, output_sha, "integrity", "source_does_not_match_frozen_plan")
        if plan.no_change or not plan.patches:
            return self._blocked(source_sha, output_sha, "integrity", "no_change_has_no_candidate")

        try:
            source_parts = _read_package(source)
            output_parts = _read_package(candidate)
            source_sheets = _sheet_parts(source_parts)
            output_sheets = _sheet_parts(output_parts)
        except Exception as exc:
            return self._blocked(source_sha, output_sha, "integrity", f"xlsx_reopen_failed:{type(exc).__name__}:{exc}")

        results: list[CheckResult] = [CheckResult(
            "integrity", "PASS", True,
            {"zip_members": len(output_parts), "xml_package_parsed": True, "duplicate_members": False},
        )]

        changed_parts = {
            name for name in source_parts.keys() | output_parts.keys()
            if name not in source_parts or name not in output_parts or source_parts[name] != output_parts[name]
        }
        part_permissions: dict[str, list] = {}
        for permission in plan.permitted_changes:
            part_permissions.setdefault(permission.part, []).append(permission)
        unlisted = sorted(changed_parts - part_permissions.keys())
        policy_errors = _check_plan_permissions(plan, source_sheets, source_sha)
        if policy_errors:
            results.append(CheckResult("policy", "FAIL", True, {"errors": policy_errors, "frozen_plan_sha256": plan.plan_sha256}))
        else:
            results.append(CheckResult("policy", "PASS", True, {
                "frozen_plan_sha256": plan.plan_sha256, "patch_count": len(plan.patches),
                "all_targets_authorized": True,
            }))

        if unlisted:
            preservation_errors = [f"unlisted_changed_part:{part}" for part in unlisted]
        else:
            preservation_errors = _verify_authorized_parts(source_parts, output_parts, plan)
        results.append(CheckResult(
            "preservation", "FAIL" if preservation_errors else "PASS", True,
            {"changed_parts": sorted(changed_parts), "part_contents_preserved": not preservation_errors,
             "details": preservation_errors,
             "shared_strings_unchanged": source_parts.get("xl/sharedStrings.xml") == output_parts.get("xl/sharedStrings.xml")},
        ))

        intent_errors: list[str] = []
        try:
            book = load_workbook(io.BytesIO(candidate), data_only=False, read_only=False, keep_links=True)
            for patch in plan.patches:
                address = _target_address(patch.target_id, source_sheets)
                if address is None:
                    intent_errors.append(f"{patch.target_id}:invalid_target_id")
                    continue
                sheet, cell_ref, part = address
                value_cell = book[sheet][cell_ref]
                if not _value_matches(value_cell.value, value_cell.data_type, value_cell.is_date, patch.new_value):
                    intent_errors.append(f"{patch.target_id}:value_or_type_mismatch")
                old_cell = _find_cell(_parse_xml(source_parts[part]), cell_ref)
                new_cell = _find_cell(_parse_xml(output_parts[part]), cell_ref)
                if old_cell is None or new_cell is None:
                    intent_errors.append(f"{patch.target_id}:cell_missing")
                elif old_cell.get("s") != new_cell.get("s"):
                    intent_errors.append(f"{patch.target_id}:style_changed")
        except Exception as exc:
            intent_errors.append(f"independent_readback_failed:{type(exc).__name__}:{exc}")
        results.append(CheckResult("intent", "FAIL" if intent_errors else "PASS", True, {"errors": intent_errors}))

        expected_provenance = plan.provenance_dict() | {
            "source_sha256": plan.source_sha256,
            "patch_plan_sha256": plan.plan_sha256,
            "adapter": "xlsx",
            "adapter_version": "0.1.0",
        }
        provenance_errors = _verify_provenance(
            output_parts, expected_provenance, custom_part_existed="docProps/custom.xml" in source_parts,
        )
        results.append(CheckResult("provenance", "FAIL" if provenance_errors else "PASS", True,
                                   {"errors": provenance_errors, "expected": expected_provenance}))

        if plan.dependents or plan.dependencies_unknown:
            results.append(CheckResult("dependents", "NOT_VERIFIED", False,
                                       {"known_dependents": list(plan.dependents), "unknown": plan.dependencies_unknown,
                                        "recalculation_requested": _calc_requested(output_parts)}))
        else:
            results.append(CheckResult("dependents", "PASS", False, {"known_dependents": [], "unknown": False}))

        critical_fail = any(c.critical and c.status != "PASS" for c in results)
        any_fail = any(c.status == "FAIL" for c in results)
        any_unverified = any(c.status == "NOT_VERIFIED" for c in results)
        outcome = "BLOCKED" if critical_fail or any_fail else "INCOMPLETE" if any_unverified else "VERIFIED"
        return VerifyReport(tuple(results), outcome, source_sha, output_sha)

    @staticmethod
    def _blocked(source_sha: str, output_sha: str, check: str, detail: str) -> VerifyReport:
        return VerifyReport((CheckResult(check, "FAIL", True, {"reason": detail}),), "BLOCKED", source_sha, output_sha)


def _plan_digest(plan: PatchPlan) -> str:
    """Verifier-local digest implementation; deliberately does not call the planner."""
    if not plan.patches:
        body = {"source_sha256": plan.source_sha256, "patches": [], "provenance": plan.provenance}
    else:
        body = {
            "source_sha256": plan.source_sha256,
            "patches": [{
                "source_version_id": patch.source_version_id, "target_id": patch.target_id,
                "expected_old_value": patch.expected_old_value.canonical(),
                "expected_fingerprint": patch.expected_fingerprint, "new_value": patch.new_value.canonical(),
            } for patch in plan.patches],
            "permissions": [asdict(permission) for permission in plan.permitted_changes],
            "provenance": plan.provenance,
            "dependents": list(plan.dependents),
            "dependencies_unknown": plan.dependencies_unknown,
            "no_change": plan.no_change,
        }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _check_plan_permissions(plan: PatchPlan, source_sheets: dict[str, str], source_sha: str) -> list[str]:
    errors: list[str] = []
    expected: set[tuple[str, str]] = set()
    for patch in plan.patches:
        if patch.source_version_id != source_sha:
            errors.append(f"{patch.target_id}:source_version_mismatch")
        address = _target_address(patch.target_id, source_sheets)
        if address is None:
            errors.append(f"{patch.target_id}:target_does_not_resolve")
            continue
        _, cell_ref, part = address
        expected.add((part, cell_ref))
    actual = {(p.part, p.locator or "") for p in plan.permitted_changes if p.operation == "replace_value"}
    if expected != actual:
        errors.append("permitted_target_set_mismatch")
    for permission in plan.permitted_changes:
        if permission.part not in source_sheets.values() and permission.part not in {
            "xl/workbook.xml", "docProps/custom.xml", "_rels/.rels", "[Content_Types].xml",
        }:
            errors.append(f"permission_for_unknown_part:{permission.part}")
    return errors


def _read_package(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [info.filename for info in archive.infolist()]
        if len(names) != len(set(names)):
            raise ValueError("duplicate_zip_member")
        corrupt = archive.testzip()
        if corrupt:
            raise ValueError(f"corrupt_zip_member:{corrupt}")
        parts = {name: archive.read(name) for name in names}
    if "xl/workbook.xml" not in parts or "xl/_rels/workbook.xml.rels" not in parts:
        raise ValueError("workbook_parts_missing")
    # Parse every XML package member, independently of the writer parser.
    for name, content in parts.items():
        if name.endswith((".xml", ".rels")) or name == "[Content_Types].xml":
            _parse_xml(content)
    return parts


def _parse_xml(data: bytes) -> ET.Element:
    return ET.fromstring(data)


def _sheet_parts(parts: dict[str, bytes]) -> dict[str, str]:
    workbook = _parse_xml(parts["xl/workbook.xml"])
    relationships = _parse_xml(parts["xl/_rels/workbook.xml.rels"])
    rel_targets = {rel.get("Id"): rel.get("Target", "") for rel in relationships.findall(f"{{{PACKAGE_REL_NS}}}Relationship")}
    result: dict[str, str] = {}
    sheets = workbook.find(f"{{{MAIN_NS}}}sheets")
    if sheets is None:
        raise ValueError("sheets_missing")
    for sheet in sheets.findall(f"{{{MAIN_NS}}}sheet"):
        name = sheet.get("name", "")
        target = rel_targets.get(sheet.get(f"{{{OFFICE_REL_NS}}}id"), "")
        if not name or not target:
            raise ValueError("invalid_sheet_relationship")
        path = _resolve_part_target("xl/workbook.xml", target)
        if path not in parts:
            raise ValueError(f"sheet_part_missing:{path}")
        result[name] = path
    if not result:
        raise ValueError("sheets_missing")
    return result


def _resolve_part_target(base: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    parent = str(PurePosixPath(base).parent)
    pieces: list[str] = []
    for part in f"{parent}/{target}".split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if pieces:
                pieces.pop()
            continue
        pieces.append(part)
    return "/".join(pieces)


def _target_address(target_id: str, sheets: dict[str, str]) -> tuple[str, str, str] | None:
    if not target_id.startswith("xlsx:"):
        return None
    address = target_id[5:]
    for sheet in sorted(sheets, key=len, reverse=True):
        prefix = f"{sheet}!"
        if address.startswith(prefix):
            cell = address[len(prefix):]
            if CELL_ADDRESS.fullmatch(cell):
                return sheet, cell, sheets[sheet]
    return None


def _find_cell(root: ET.Element, cell_ref: str) -> ET.Element | None:
    found = [node for node in root.iter(f"{{{MAIN_NS}}}c") if node.get("r") == cell_ref]
    return found[0] if len(found) == 1 else None


def _canonical(root: ET.Element) -> str:
    xml_text = ET.tostring(root, encoding="unicode")
    return ET.canonicalize(xml_data=xml_text, with_comments=True, strip_text=False)


def _verify_authorized_parts(source_parts: dict[str, bytes], output_parts: dict[str, bytes], plan: PatchPlan) -> list[str]:
    errors: list[str] = []
    permission_by_part: dict[str, list] = {}
    for permission in plan.permitted_changes:
        permission_by_part.setdefault(permission.part, []).append(permission)
    source_sheets = _sheet_parts(source_parts)

    for part in set(source_parts) & set(output_parts):
        if source_parts[part] == output_parts[part]:
            continue
        allowed = permission_by_part.get(part, [])
        if part.startswith("xl/worksheets/"):
            try:
                old, new = _parse_xml(source_parts[part]), _parse_xml(output_parts[part])
                refs = {p.locator for p in allowed if p.operation == "replace_value" and p.locator}
                if not refs:
                    errors.append(f"{part}:no_cell_permission")
                    continue
                for ref in refs:
                    patch_values = []
                    for patch in plan.patches:
                        address = _target_address(patch.target_id, source_sheets)
                        if address is not None and address[1:] == (ref, part):
                            patch_values.append(patch.new_value)
                    old_cell = _find_cell(old, ref)
                    new_cell = _find_cell(new, ref)
                    if len(patch_values) == 1 and new_cell is not None:
                        errors.extend(f"{part}:{ref}:{reason}" for reason in _approved_cell_errors(
                            old_cell, new_cell, patch_values[0],
                        ))
                    for root in (old, new):
                        cells = [node for node in root.iter(f"{{{MAIN_NS}}}c") if node.get("r") == ref]
                        if len(cells) != 1:
                            errors.append(f"{part}:{ref}:cell_count")
                        else:
                            parent = next(parent for parent in root.iter() if cells[0] in list(parent))
                            parent.remove(cells[0])
                    if len(patch_values) != 1:
                        errors.append(f"{part}:{ref}:patch_mapping_not_unique")
                if _canonical(old) != _canonical(new):
                    errors.append(f"{part}:unapproved_sheet_xml_change")
            except Exception as exc:
                errors.append(f"{part}:xml_parse:{type(exc).__name__}")
        elif part == "xl/workbook.xml":
            try:
                old, new = _parse_xml(source_parts[part]), _parse_xml(output_parts[part])
                allowed_calc = any(p.locator == "calcPr" and p.operation == "set_attribute" for p in allowed)
                if allowed_calc:
                    old_calc = old.find(f"{{{MAIN_NS}}}calcPr")
                    new_calc = new.find(f"{{{MAIN_NS}}}calcPr")
                    if new_calc is None or new_calc.get("fullCalcOnLoad") != "1" or new_calc.get("forceFullCalc") != "1":
                        errors.append("xl/workbook.xml:calc_flag_missing")
                    if old_calc is None and new_calc is not None:
                        if set(new_calc.attrib) != {"fullCalcOnLoad", "forceFullCalc"} or len(new_calc):
                            errors.append("xl/workbook.xml:unapproved_calcPr_content")
                        new.remove(new_calc)
                    else:
                        for node in (old_calc, new_calc):
                            if node is not None:
                                node.attrib.pop("fullCalcOnLoad", None)
                                node.attrib.pop("forceFullCalc", None)
                if _canonical(old) != _canonical(new):
                    errors.append("xl/workbook.xml:unapproved_change")
            except Exception as exc:
                errors.append(f"xl/workbook.xml:xml_parse:{type(exc).__name__}")
        elif part == "docProps/custom.xml":
            try:
                old, new = _parse_xml(source_parts[part]), _parse_xml(output_parts[part])
                for root in (old, new):
                    for node in list(root):
                        if node.tag == f"{{{CUSTOM_NS}}}property" and node.get("name") == PROVENANCE_NAME:
                            root.remove(node)
                if _canonical(old) != _canonical(new):
                    errors.append("docProps/custom.xml:unapproved_custom_property_change")
            except Exception as exc:
                errors.append(f"docProps/custom.xml:xml_parse:{type(exc).__name__}")
        elif part == "_rels/.rels":
            try:
                old, new = _parse_xml(source_parts[part]), _parse_xml(output_parts[part])
                old_has_custom = any(n.get("Type") == CUSTOM_REL for n in old.findall(f"{{{PACKAGE_REL_NS}}}Relationship"))
                if not old_has_custom:
                    for node in list(new):
                        if node.get("Type") == CUSTOM_REL and node.get("Target") == "docProps/custom.xml":
                            new.remove(node)
                if _canonical(old) != _canonical(new):
                    errors.append("_rels/.rels:unapproved_relationship_change")
            except Exception as exc:
                errors.append(f"_rels/.rels:xml_parse:{type(exc).__name__}")
        elif part == "[Content_Types].xml":
            try:
                old, new = _parse_xml(source_parts[part]), _parse_xml(output_parts[part])
                custom_override = "docProps/custom.xml"
                old_has_override = any(n.get("PartName") == f"/{custom_override}" for n in old.findall(f"{{{CONTENT_NS}}}Override"))
                if not old_has_override:
                    for node in list(new):
                        if node.tag == f"{{{CONTENT_NS}}}Override" and node.get("PartName") == f"/{custom_override}":
                            new.remove(node)
                if _canonical(old) != _canonical(new):
                    errors.append("[Content_Types].xml:unapproved_content_type_change")
            except Exception as exc:
                errors.append(f"[Content_Types].xml:xml_parse:{type(exc).__name__}")

    for part in set(output_parts) - set(source_parts):
        if part not in permission_by_part:
            errors.append(f"{part}:unapproved_added_part")
    for part in set(source_parts) - set(output_parts):
        errors.append(f"{part}:removed_part")
    return errors


def _value_matches(value: Any, data_type: str, is_date: bool, expected) -> bool:
    if expected.type == "string": return isinstance(value, str) and value == expected.value and data_type == "s"
    if expected.type == "boolean": return isinstance(value, bool) and value is expected.value and data_type == "b"
    if expected.type == "integer": return isinstance(value, int) and not isinstance(value, bool) and value == expected.value and data_type == "n"
    if expected.type == "decimal": return isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) == float(expected.value) and data_type == "n"
    if expected.type == "date": return is_date and value.isoformat()[:10] == expected.value
    if expected.type == "datetime": return is_date and value.isoformat() == expected.value
    if expected.type == "null": return value is None
    return False


def _approved_cell_errors(old: ET.Element | None, new: ET.Element, expected) -> list[str]:
    """Check the authorized cell shape independently without using the writer's XML digest."""
    if old is None:
        return ["source_cell_missing"]
    errors: list[str] = []
    old_attrs = {key: value for key, value in old.attrib.items() if key != "t"}
    new_attrs = {key: value for key, value in new.attrib.items() if key != "t"}
    if old_attrs != new_attrs:
        errors.append("unapproved_cell_attribute_change")
    children = list(new)
    if expected.type == "string":
        if new.get("t") != "inlineStr" or len(children) != 1 or children[0].tag != f"{{{MAIN_NS}}}is":
            return errors + ["invalid_inline_string_shape"]
        texts = list(children[0])
        if len(texts) != 1 or texts[0].tag != f"{{{MAIN_NS}}}t" or len(texts[0]):
            return errors + ["invalid_inline_string_text_node"]
        text_node = texts[0]
        if (text_node.text or "") != expected.value:
            errors.append("inline_string_value_mismatch")
        preserve = text_node.get("{http://www.w3.org/XML/1998/namespace}space")
        should_preserve = bool(expected.value[:1].isspace() or expected.value[-1:].isspace())
        if (preserve == "preserve") != should_preserve:
            errors.append("inline_string_whitespace_policy_mismatch")
        return errors
    if expected.type == "boolean":
        if new.get("t") != "b": errors.append("boolean_cell_type_mismatch")
        raw_expected = "1" if expected.value else "0"
    elif expected.type == "null":
        if new.get("t") != "str": errors.append("null_cell_type_mismatch")
        raw_expected = ""
    elif expected.type in {"integer", "decimal", "date", "datetime"}:
        if new.get("t") is not None: errors.append("numeric_cell_type_mismatch")
        raw_expected = None
    else:
        return errors + ["unsupported_expected_cell_type"]
    if len(children) != 1 or children[0].tag != f"{{{MAIN_NS}}}v":
        return errors + ["invalid_value_node_shape"]
    raw = children[0].text or ""
    if expected.type == "boolean" and raw != raw_expected:
        errors.append("boolean_native_value_mismatch")
    elif expected.type == "null" and raw != raw_expected:
        errors.append("null_native_value_mismatch")
    elif expected.type in {"integer", "decimal"}:
        try:
            if float(raw) != float(expected.value): errors.append("numeric_native_value_mismatch")
        except ValueError:
            errors.append("numeric_native_value_invalid")
    return errors


def _verify_provenance(parts: dict[str, bytes], expected: dict[str, str], *, custom_part_existed: bool) -> list[str]:
    errors: list[str] = []
    if "docProps/custom.xml" not in parts:
        return ["custom_provenance_part_missing"]
    try:
        root = _parse_xml(parts["docProps/custom.xml"])
        if root.tag != f"{{{CUSTOM_NS}}}Properties":
            return ["invalid_provenance_root"]
        props = [node for node in root if node.tag == f"{{{CUSTOM_NS}}}property"]
        selected = [node for node in props if node.get("name") == PROVENANCE_NAME]
        if len(selected) != 1:
            return ["provenance_property_missing_or_duplicate"]
        if not custom_part_existed and (len(props) != 1 or root.attrib):
            errors.append("unapproved_content_in_new_custom_part")
        prop = selected[0]
        if prop.get("fmtid") != "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" or not prop.get("pid", "").isdigit():
            errors.append("invalid_provenance_property_metadata")
        values = [node.text or "" for node in list(prop) if node.tag == f"{{{VT_NS}}}lpwstr"]
        if len(values) != 1:
            return errors + ["provenance_value_missing_or_invalid"]
        if json.loads(values[0]) != expected:
            errors.append("provenance_value_mismatch")
    except Exception as exc:
        errors.append(f"provenance_parse_failed:{type(exc).__name__}")
    return errors


def _calc_requested(parts: dict[str, bytes]) -> bool:
    if "xl/workbook.xml" not in parts:
        return False
    calc = _parse_xml(parts["xl/workbook.xml"]).find(f"{{{MAIN_NS}}}calcPr")
    return calc is not None and calc.get("fullCalcOnLoad") == "1"
