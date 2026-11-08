from __future__ import annotations

import hashlib
import io
import copy
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

from packages.core.models.contracts import CheckResult, PatchPlan, VerifyReport
from packages.workers.office import OfficeRenderError, render_docx_pdf

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML = "http://www.w3.org/XML/1998/namespace"
CP = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
VT = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
PROVENANCE = {
    "Channel360_Source_SHA256": "source_sha256",
    "Channel360_Edit_Set": "edit_set_id",
    "Channel360_Patch_Plan_SHA256": "patch_plan_sha256",
}
TARGET_RE = re.compile(r"^docx:(.+):p(\d+):r(\d+):t(\d+)$")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _xml(data: bytes) -> ET.Element:
    return ET.fromstring(data)


def _canonical(root: ET.Element) -> str:
    return ET.canonicalize(ET.tostring(root, encoding="unicode"), with_comments=True)


def _element_fingerprint(node: ET.Element) -> str:
    return _hash(ET.canonicalize(ET.tostring(node, encoding="unicode"), with_comments=True).encode())


def _parts(data: bytes) -> tuple[list[tuple[str, int]], dict[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)) or archive.testzip():
            raise ValueError("invalid_package")
        return [(item.filename, item.compress_type) for item in infos], {name: archive.read(name) for name in names}


def _target_node(root: ET.Element, target_id: str) -> ET.Element:
    match = TARGET_RE.fullmatch(target_id)
    if not match:
        raise ValueError("invalid_target_id")
    part, p_index, r_index, t_index = match.groups()
    if part not in {"word/document.xml"} and not re.fullmatch(r"word/(?:header|footer)\d+\.xml", part):
        raise ValueError("unsupported_target_part")
    paragraphs = root.findall(f".//{{{W}}}p")
    paragraph = paragraphs[int(p_index)]
    runs = paragraph.findall(f".//{{{W}}}r")
    run = runs[int(r_index)]
    return run.findall(f"{{{W}}}t")[int(t_index)]


def _normalize_target_part(data: bytes, targets: list[str]) -> str:
    root = _xml(data)
    for target_id in targets:
        node = _target_node(root, target_id)
        node.text = "__CHANNEL360_EDITED_TEXT__"
        node.attrib.pop(f"{{{XML}}}space", None)
    return _canonical(root)


def _qpdfium_text(pdf: bytes) -> list[str]:
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(pdf)
    try:
        output = []
        for page in document:
            text = page.get_textpage().get_text_range()
            output.append(text.replace("\r\n", "\n").replace("\r", "\n"))
        return output
    finally:
        document.close()


class DocxIndependentVerifier:
    """OOXML part verifier plus mandatory isolated LibreOffice reflow/render checks."""

    def verify(self, source: bytes, candidate: bytes, plan: PatchPlan) -> VerifyReport:
        source_hash, output_hash = _hash(source), _hash(candidate)
        if (plan.source_sha256 != source_hash or plan.source_version_id != source_hash or plan.no_change
                or _plan_hash(plan) != plan.plan_sha256):
            return _blocked(source_hash, output_hash, "frozen_plan_or_source_mismatch")
        try:
            old_order, old_parts = _parts(source)
            new_order, new_parts = _parts(candidate)
        except Exception:
            return _blocked(source_hash, output_hash, "malformed_ooxml")
        if [name for name, _ in old_order] != [name for name, _ in new_order[:len(old_order)]]:
            return _blocked(source_hash, output_hash, "zip_member_order_changed")
        for name, method in old_order:
            if next((kind for part, kind in new_order if part == name), None) != method:
                return _blocked(source_hash, output_hash, "zip_compression_method_changed")

        target_ids = [patch.target_id for patch in plan.patches]
        target_parts: dict[str, list[str]] = {}
        intent_errors: list[str] = []
        for patch in plan.patches:
            match = TARGET_RE.fullmatch(patch.target_id)
            if not match:
                intent_errors.append(f"{patch.target_id}:invalid_target_id")
                continue
            part = match.group(1)
            target_parts.setdefault(part, []).append(patch.target_id)
            try:
                old_root, new_root = _xml(old_parts[part]), _xml(new_parts[part])
                old_node, new_node = _target_node(old_root, patch.target_id), _target_node(new_root, patch.target_id)
                if _element_fingerprint(old_node) != patch.expected_fingerprint:
                    intent_errors.append(f"{patch.target_id}:source_fingerprint_mismatch")
                if old_node.text != str(patch.expected_old_value.value):
                    intent_errors.append(f"{patch.target_id}:expected_value_mismatch")
                if new_node.text != str(patch.new_value.value):
                    intent_errors.append(f"{patch.target_id}:new_value_mismatch")
            except Exception:
                intent_errors.append(f"{patch.target_id}:target_missing_or_invalid")

        # Canonical part equality after all target substitutions verifies the
        # run/paragraph properties and every non-target XML node.
        for part, ids in target_parts.items():
            try:
                if _normalize_target_part(old_parts[part], ids) != _normalize_target_part(new_parts[part], ids):
                    intent_errors.append(f"{part}:non_target_xml_changed")
            except Exception:
                intent_errors.append(f"{part}:canonical_xml_check_failed")

        expected_changes = {part for part in target_parts} | {"docProps/custom.xml", "[Content_Types].xml", "_rels/.rels"}
        changed = {name for name in old_parts.keys() | new_parts.keys()
                   if old_parts.get(name) != new_parts.get(name)}
        preservation_errors = sorted(changed - expected_changes)
        # Permission inventory must name every changed/addition and cannot expand the actual change boundary.
        permission_parts = {permission.part for permission in plan.permitted_changes}
        policy_errors = sorted((changed - permission_parts) | (permission_parts - expected_changes))
        if not set(target_parts).issubset(new_parts):
            preservation_errors.append("target_part_missing")

        provenance_errors = _verify_provenance(new_parts, plan)
        results = [
            CheckResult("integrity", "PASS", True, {"zip_valid": True, "member_order_preserved": True,
                                                      "compression_methods_preserved": True}),
            CheckResult("policy", "PASS" if not policy_errors else "FAIL", True,
                        {"errors": policy_errors, "changed_parts": sorted(changed)}),
            CheckResult("intent", "PASS" if not intent_errors else "FAIL", True, {"errors": intent_errors}),
            CheckResult("preservation", "PASS" if not preservation_errors else "FAIL", True,
                        {"errors": preservation_errors}),
            CheckResult("provenance", "PASS" if not provenance_errors else "FAIL", True,
                        {"errors": provenance_errors}),
        ]
        try:
            old_pdf, new_pdf = render_docx_pdf(source), render_docx_pdf(candidate)
            old_text, new_text = _qpdfium_text(old_pdf), _qpdfium_text(new_pdf)
            if len(old_text) != len(new_text):
                visual_errors = ["page_count_changed"]
            else:
                expected_text = new_text[:]
                for patch in plan.patches:
                    expected_text = [text.replace(str(patch.new_value.value), str(patch.expected_old_value.value))
                                     for text in expected_text]
                visual_errors = [] if old_text == expected_text else ["paragraph_line_breaks_or_layout_changed"]
            results.append(CheckResult("visual", "PASS" if not visual_errors else "FAIL", True,
                                       {"renderer": "LibreOffice + pypdfium2", "page_count": len(old_text),
                                        "errors": visual_errors}))
        except (OfficeRenderError, Exception) as exc:
            results.append(CheckResult("visual", "NOT_VERIFIED", True,
                                       {"reason": f"critical_render_check_unavailable:{type(exc).__name__}"}))
        outcome = "BLOCKED" if any(item.critical and item.status != "PASS" for item in results) else "VERIFIED"
        return VerifyReport(tuple(results), outcome, source_hash, output_hash)


def _plan_hash(plan: PatchPlan) -> str:
    from dataclasses import asdict
    if not plan.patches:
        body = {"source_sha256": plan.source_sha256, "patches": [], "provenance": plan.provenance}
    else:
        body = {"source_sha256": plan.source_sha256,
                "patches": [{"source_version_id": item.source_version_id, "target_id": item.target_id,
                             "expected_old_value": item.expected_old_value.canonical(),
                             "expected_fingerprint": item.expected_fingerprint, "new_value": item.new_value.canonical()}
                            for item in plan.patches],
                "permissions": [asdict(permission) for permission in plan.permitted_changes],
                "provenance": plan.provenance, "dependents": list(plan.dependents),
                "dependencies_unknown": plan.dependencies_unknown, "no_change": plan.no_change}
    return _hash(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())


def _verify_provenance(parts: dict[str, bytes], plan: PatchPlan) -> list[str]:
    try:
        root = _xml(parts["docProps/custom.xml"])
    except Exception:
        return ["custom_properties_missing_or_invalid"]
    values: dict[str, str] = {}
    for prop in root.findall(f"{{{CP}}}property"):
        name = prop.get("name")
        child = next(iter(prop), None)
        if name and child is not None:
            values[name] = child.text or ""
    expected = plan.provenance_dict() | {"source_sha256": plan.source_sha256,
                                        "patch_plan_sha256": plan.plan_sha256}
    return [f"{name}:mismatch" for name, key in PROVENANCE.items() if values.get(name) != expected.get(key, "")]


def _blocked(source_hash: str, output_hash: str, reason: str) -> VerifyReport:
    return VerifyReport((CheckResult("integrity", "FAIL", True, {"reason": reason}),),
                        "BLOCKED", source_hash, output_hash)
