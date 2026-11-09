"""Offline structural diagnostics for DOCX candidates; never a release verifier."""
from __future__ import annotations

import hashlib
import json
import zipfile
from typing import Any

from lxml import etree

from packages.core.adapters.docx.adapter import (
    CUSTOM_REL, NS, DocxAdapter, _node_at, _parse, _parts,
)


_ALLOWED_PARTS = {"[Content_Types].xml", "_rels/.rels", "docProps/custom.xml"}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _archive(data: bytes) -> tuple[list[zipfile.ZipInfo], dict[str, bytes]]:
    ordered, parts = _parts(data)
    return [info for info, _ in ordered], parts


def _c14n(xml: bytes) -> bytes:
    return etree.tostring(_parse(xml), method="c14n", exclusive=False, with_comments=True)


def _restore_target_values(xml: bytes, part: str, changes: list[dict[str, Any]], adapter: DocxAdapter,
                           source: bytes) -> bytes:
    root = _parse(xml)
    source_targets = {target.id: target for target in adapter.targets(source)}
    for change in changes:
        target = source_targets[change["target_id"]]
        if target.part != part:
            continue
        node = _node_at(root, target.native_locator or {})
        node.text = change["old_value"]
    return etree.tostring(root, method="c14n", exclusive=False, with_comments=True)


def _strip_added_custom_content_type(xml: bytes) -> bytes:
    root = _parse(xml)
    for node in root.xpath("./ct:Override[@PartName='/docProps/custom.xml']", namespaces=NS):
        root.remove(node)
    return etree.tostring(root, method="c14n", exclusive=False, with_comments=True)


def _strip_custom_relationship(xml: bytes) -> bytes:
    root = _parse(xml)
    for node in root.xpath("./rel:Relationship[@Type=$kind and @Target='docProps/custom.xml']",
                           namespaces=NS, kind=CUSTOM_REL):
        root.remove(node)
    return etree.tostring(root, method="c14n", exclusive=False, with_comments=True)


def _provenance(parts: dict[str, bytes]) -> dict[str, str]:
    raw = parts.get("docProps/custom.xml")
    if raw is None:
        return {}
    root = _parse(raw)
    values: dict[str, str] = {}
    for prop in root.xpath("./cp:property", namespaces=NS):
        name = prop.get("name")
        if name:
            values[name] = "".join(prop.itertext())
    return values


def _target_manifest_check(source: bytes, candidate: bytes, expected: dict[str, Any]) -> dict[str, Any]:
    adapter = DocxAdapter()
    source_targets = {target.id: target for target in adapter.targets(source)}
    candidate_targets = {target.id: target for target in adapter.targets(candidate)}
    errors: list[str] = []
    seen: set[str] = set()
    for change in expected.get("changes", []):
        target_id = change.get("target_id")
        if not target_id or target_id in seen:
            errors.append("missing_or_duplicate_target_id")
            continue
        seen.add(target_id)
        old_target, new_target = source_targets.get(target_id), candidate_targets.get(target_id)
        if old_target is None or new_target is None:
            errors.append(f"target_missing:{target_id}")
            continue
        if not old_target.editable:
            errors.append(f"source_target_not_editable:{target_id}:{old_target.reason}")
        if old_target.value.value != change.get("old_value"):
            errors.append(f"old_value_mismatch:{target_id}")
        if old_target.fingerprint != change.get("target_fingerprint"):
            errors.append(f"target_fingerprint_mismatch:{target_id}")
        if new_target.value.value != change.get("new_value"):
            errors.append(f"new_value_mismatch:{target_id}")
        if old_target.part != change.get("part", old_target.part) or new_target.part != old_target.part:
            errors.append(f"target_part_mismatch:{target_id}")
    if not seen:
        errors.append("no_expected_changes")
    return {"status": "PASS" if not errors else "FAIL", "errors": errors,
            "target_count": len(seen)}


def validate_candidate(original: bytes, candidate: bytes, expected: dict[str, Any], *,
                       rendered_original_pdf: bytes | None = None,
                       rendered_candidate_pdf: bytes | None = None) -> dict[str, Any]:
    """Return diagnostic checks. A structural PASS never authorizes release."""
    source_hash, candidate_hash = _sha256(original), _sha256(candidate)
    hash_errors = []
    if source_hash != expected.get("source_sha256"):
        hash_errors.append("source_sha256_mismatch")
    if candidate_hash != expected.get("candidate_sha256"):
        hash_errors.append("candidate_sha256_mismatch")
    hashes_check = {"status": "PASS" if not hash_errors else "FAIL", "errors": hash_errors}

    checks: dict[str, Any] = {"hashes": hashes_check}
    try:
        original_infos, original_parts = _archive(original)
        candidate_infos, candidate_parts = _archive(candidate)
        checks["zip_integrity"] = {"status": "PASS", "original_parts": len(original_infos),
                                   "candidate_parts": len(candidate_infos)}
    except (ValueError, zipfile.BadZipFile, OSError) as exc:
        failed = {"status": "FAIL", "errors": [f"archive_invalid:{type(exc).__name__}:{exc}"]}
        checks.update({"zip_integrity": failed.copy(), "target_values_fingerprints": failed.copy(),
                       "unexpected_package_changes": failed.copy(), "provenance": failed.copy(),
                       "formatting_styles_tables": failed.copy()})
        checks["visual"] = {"status": "NOT_VERIFIED", "reason": "invalid_archive"}
        return _report(source_hash, candidate_hash, checks)

    target_check = _target_manifest_check(original, candidate, expected)
    checks["target_values_fingerprints"] = target_check

    original_names = [info.filename for info in original_infos]
    candidate_names = [info.filename for info in candidate_infos]
    order_errors = []
    if candidate_names[:len(original_names)] != original_names:
        order_errors.append("original_package_member_order_not_preserved")
    compression_errors = [info.filename for info, after in zip(original_infos, candidate_infos)
                          if info.filename in candidate_parts and info.compress_type != next(
                              item.compress_type for item in candidate_infos if item.filename == info.filename)]
    if compression_errors:
        order_errors.extend(f"compression_method_changed:{name}" for name in compression_errors)

    changed = {name for name in original_parts.keys() | candidate_parts.keys()
               if original_parts.get(name) != candidate_parts.get(name)}
    allowed = set(_ALLOWED_PARTS) | {change.get("part", "word/document.xml")
                                     for change in expected.get("changes", [])}
    unexpected = sorted(changed - allowed)
    if unexpected:
        order_errors.extend(f"unexpected_changed_part:{name}" for name in unexpected)
    added = sorted(candidate_parts.keys() - original_parts.keys())
    removed = sorted(original_parts.keys() - candidate_parts.keys())
    if removed:
        order_errors.extend(f"removed_part:{name}" for name in removed)
    if any(name not in _ALLOWED_PARTS for name in added):
        order_errors.extend(f"unexpected_added_part:{name}" for name in added if name not in _ALLOWED_PARTS)

    # Normalize only the explicitly authorized text targets and provenance relationship entries.
    adapter = DocxAdapter()
    xml_comparisons: dict[str, bool] = {}
    for name in changed & allowed:
        if name in original_parts and name in candidate_parts:
            try:
                if name in {"[Content_Types].xml", "_rels/.rels"}:
                    normalize = _strip_added_custom_content_type if name == "[Content_Types].xml" else _strip_custom_relationship
                    if normalize(original_parts[name]) != normalize(candidate_parts[name]):
                        order_errors.append(f"unexpected_metadata_change:{name}")
                elif name.startswith("word/") and name.endswith(".xml"):
                    restored = _restore_target_values(candidate_parts[name], name,
                                                       expected.get("changes", []), adapter, original)
                    same_after_edits = _c14n(original_parts[name]) == restored
                    xml_comparisons[name] = same_after_edits
                    if not same_after_edits:
                        order_errors.append(f"unexpected_xml_change:{name}")
            except (KeyError, IndexError, ValueError, etree.XMLSyntaxError) as exc:
                order_errors.append(f"could_not_normalize_changed_part:{name}:{type(exc).__name__}")
    checks["unexpected_package_changes"] = {"status": "PASS" if not order_errors else "FAIL",
                                             "errors": order_errors, "changed_parts": sorted(changed),
                                             "added_parts": added, "removed_parts": removed}
    body_xml_checked = "word/document.xml" in original_parts and "word/document.xml" in candidate_parts
    body_xml_equal = xml_comparisons.get("word/document.xml", original_parts.get("word/document.xml") ==
                                        candidate_parts.get("word/document.xml"))
    checks["document_text_and_tables"] = {
        "status": "PASS" if body_xml_checked and body_xml_equal else "FAIL",
        "errors": [] if body_xml_checked and body_xml_equal else ["body_table_xml_changed_outside_expected_targets"],
        "method": "canonical OOXML comparison after restoring expected target values",
    }

    provenance = _provenance(candidate_parts)
    required_provenance = {
        "Channel360_Source_SHA256": source_hash,
        "Channel360_Edit_Set": expected.get("edit_set_id", "offline-docx-manual-comparison"),
        "Channel360_Patch_Plan_SHA256": expected.get("patch_plan_sha256"),
    }
    provenance_errors = [f"provenance_mismatch:{name}" for name, value in required_provenance.items()
                         if value is None or provenance.get(name) != value]
    content_types = _parse(candidate_parts.get("[Content_Types].xml", b"<Types/>"))
    custom_overrides = content_types.xpath("./ct:Override[@PartName='/docProps/custom.xml']", namespaces=NS)
    expected_content_type = "application/vnd.openxmlformats-officedocument.custom-properties+xml"
    if len(custom_overrides) != 1 or custom_overrides[0].get("ContentType") != expected_content_type:
        provenance_errors.append("invalid_custom_properties_content_type")
    relationships = _parse(candidate_parts.get("_rels/.rels", b"<Relationships/>"))
    custom_relationships = relationships.xpath(
        "./rel:Relationship[@Type=$kind and @Target='docProps/custom.xml']", namespaces=NS, kind=CUSTOM_REL)
    if len(custom_relationships) != 1 or not custom_relationships[0].get("Id"):
        provenance_errors.append("invalid_custom_properties_relationship")
    ids = [node.get("Id") for node in relationships.xpath("./rel:Relationship", namespaces=NS)]
    if len(ids) != len(set(ids)):
        provenance_errors.append("duplicate_relationship_id")
    checks["provenance"] = {"status": "PASS" if not provenance_errors else "FAIL",
                            "errors": provenance_errors, "properties": provenance}

    # This check is intentionally explicit: the canonical document tree comparison above covers
    # paragraph/run properties, table structure/text, while these parts must remain byte-identical.
    stable_style_parts = sorted(name for name in original_parts if name.startswith(("word/styles", "word/fontTable",
                                  "word/numbering", "word/theme", "word/media/")))
    style_errors = [f"protected_content_part_changed:{name}" for name in stable_style_parts
                    if original_parts[name] != candidate_parts.get(name)]
    for name in set(original_parts) & set(candidate_parts):
        if name.startswith("word/") and name.endswith(".xml") and name not in allowed \
                and original_parts[name] != candidate_parts[name]:
            style_errors.append(f"unrelated_word_part_changed:{name}")
    checks["formatting_styles_tables"] = {"status": "PASS" if not style_errors else "FAIL",
                                          "errors": style_errors,
                                          "stable_parts_checked": stable_style_parts}

    if rendered_original_pdf is None or rendered_candidate_pdf is None:
        checks["visual"] = {"status": "NOT_VERIFIED", "reason": "rendered_pdf_evidence_not_supplied"}
    else:
        from packages.core.verification.docx import _compare_rendered_layout
        replacements = [(change["old_value"], change["new_value"])
                        for change in expected.get("changes", [])]
        visual = _compare_rendered_layout(rendered_original_pdf, rendered_candidate_pdf, replacements)
        checks["visual"] = {"status": "PASS" if not visual["errors"] else "FAIL", **visual}
    report = _report(source_hash, candidate_hash, checks)
    supplied_evidence = expected.get("external_visual_evidence")
    if supplied_evidence:
        report["external_evidence"] = {
            "status": "USER_SUPPLIED_NOT_REPRODUCED",
            "summary": supplied_evidence.get("summary"),
            "renderer": supplied_evidence.get("renderer"),
            "is_application_release_verification": False,
            "local_visual_status": report["visual_status"],
        }
    return report


def _report(source_hash: str, candidate_hash: str, checks: dict[str, Any]) -> dict[str, Any]:
    structural = ["hashes", "zip_integrity", "target_values_fingerprints", "unexpected_package_changes",
                  "document_text_and_tables", "provenance", "formatting_styles_tables"]
    failures = [name for name in structural if checks.get(name, {}).get("status") != "PASS"]
    structural_status = "FAIL" if failures else "PASS"
    visual_status = checks.get("visual", {}).get("status", "NOT_VERIFIED")
    overall = "FAIL" if failures or visual_status == "FAIL" else (
        "NOT_VERIFIED" if visual_status != "PASS" else "DIAGNOSTIC_PASS_NOT_A_RELEASE")
    return {"report_type": "offline_docx_candidate_diagnostic",
            "overall_status": overall, "structural_status": structural_status,
            "visual_status": visual_status, "release_authorized": False,
            "hashes": {"source": source_hash, "candidate": candidate_hash},
            "checks": checks}


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# Offline DOCX Candidate Diagnostic", "",
             f"- Overall: **{report['overall_status']}**",
             f"- Structural: **{report['structural_status']}**",
             f"- Visual: **{report['visual_status']}**",
             "- Release authorized: **NO**", "",
             "This diagnostic does not approve, verify, register, or release a derivative.", "",
             "| Check | Result | Details |", "| --- | --- | --- |"]
    for name, check in report["checks"].items():
        details = json.dumps({key: value for key, value in check.items() if key != "status"}, sort_keys=True)
        lines.append(f"| {name} | {check.get('status', 'NOT_VERIFIED')} | `{details}` |")
    evidence = report.get("external_evidence")
    if evidence:
        lines.extend(["", "## Supplied external evidence", "",
                      f"- Status: **{evidence['status']}** (not reproduced locally)",
                      f"- Renderer: {evidence.get('renderer')}",
                      f"- Summary: {evidence.get('summary')}",
                      "- This does not change the local visual status or authorize an application release."])
    return "\n".join(lines) + "\n"
