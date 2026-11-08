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


def _qpdfium_layout(pdf: bytes) -> list[list[tuple[str, tuple[float, float, float, float] | None]]]:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf)
    try:
        output: list[list[tuple[str, tuple[float, float, float, float] | None]]] = []
        for page in document:
            text_page = page.get_textpage()
            text = text_page.get_text_range()
            if text_page.count_chars() != len(text):
                raise ValueError("pdfium_text_geometry_length_mismatch")
            glyphs = []
            for index, char in enumerate(text):
                try:
                    raw_box = text_page.get_charbox(index)
                    box = tuple(float(value) for value in raw_box) if raw_box is not None else None
                except (IndexError, ValueError):
                    box = None
                glyphs.append((char, box))
            output.append(glyphs)
        return output
    finally:
        document.close()


def _qpdfium_rendered_pages(pdf: bytes):
    """Render PDF pages to RGB images for critical non-text visual checks."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf)
    try:
        rendered = []
        for page in document:
            bitmap = page.render(scale=1.0)
            rendered.append((bitmap.to_pil().convert("RGB"), float(page.get_width()), float(page.get_height())))
            bitmap.close()
        return rendered
    finally:
        document.close()


def _compare_rendered_pixels(source_pdf: bytes, candidate_pdf: bytes,
                             old_glyphs, new_glyphs, old_spans, new_spans,
                             *, padding_points: float = 4.0) -> dict[str, Any]:
    from PIL import Image, ImageChops, ImageDraw

    old_pages = _qpdfium_rendered_pages(source_pdf)
    new_pages = _qpdfium_rendered_pages(candidate_pdf)
    errors: list[str] = []
    if len(old_pages) != len(new_pages):
        return {"errors": ["page_count_changed"], "pixels_changed_outside_targets": None,
                "pixels_compared_outside_targets": 0}
    target_boxes: list[list[tuple[float, float, float, float]]] = [[] for _ in old_pages]
    for glyphs, spans in ((old_glyphs, old_spans), (new_glyphs, new_spans)):
        for start, end in spans:
            boxes = [glyphs[index] for index in range(start, end)
                     if glyphs[index] is not None and glyphs[index][2] is not None]
            if not boxes:
                errors.append("target_pixel_region_unavailable")
                continue
            pages = {entry[0] for entry in boxes}
            if len(pages) != 1:
                errors.append("target_crosses_page_boundary")
                continue
            page_number = next(iter(pages))
            rects = [entry[2] for entry in boxes]
            target_boxes[page_number].append((min(box[0] for box in rects) - padding_points,
                                              min(box[1] for box in rects) - padding_points,
                                              max(box[2] for box in rects) + padding_points,
                                              max(box[3] for box in rects) + padding_points))
    if errors:
        return {"errors": sorted(set(errors)), "pixels_changed_outside_targets": None,
                "pixels_compared_outside_targets": 0}

    outside_changed = 0
    outside_compared = 0
    for page_number, ((old_image, old_width, old_height), (new_image, new_width, new_height)) in enumerate(
            zip(old_pages, new_pages)):
        if old_image.size != new_image.size or old_width != new_width or old_height != new_height:
            errors.append("render_dimensions_changed")
            continue
        allowed = Image.new("L", old_image.size, 255)
        draw = ImageDraw.Draw(allowed)
        scale_x, scale_y = old_image.width / old_width, old_image.height / old_height
        for left, bottom, right, top in target_boxes[page_number]:
            rectangle = (max(0, int((left) * scale_x)),
                         max(0, int((old_height - top) * scale_y)),
                         min(old_image.width, int((right) * scale_x) + 1),
                         min(old_image.height, int((old_height - bottom) * scale_y) + 1))
            draw.rectangle(rectangle, fill=0)
        difference = ImageChops.difference(old_image, new_image).convert("L")
        outside = Image.composite(difference, Image.new("L", difference.size, 0), allowed)
        outside_changed += sum(outside.histogram()[1:])
        outside_compared += allowed.histogram()[255]
    if outside_changed:
        errors.append("outside_visual_pixels_changed")
    return {"errors": sorted(set(errors)), "pixels_changed_outside_targets": outside_changed,
            "pixels_compared_outside_targets": outside_compared,
            "target_region_padding_points": padding_points}


def _flatten_layout(layout: list[list[tuple[str, tuple[float, float, float, float] | None]]]):
    chars: list[str] = []
    glyphs: list[tuple[int, str, tuple[float, float, float, float] | None] | None] = []
    for page_number, page in enumerate(layout):
        if page_number:
            chars.append("\f")
            glyphs.append(None)
        for char, box in page:
            chars.append(char)
            glyphs.append((page_number, char, box))
    return "".join(chars), glyphs


def _occurrences(text: str, value: str) -> list[int]:
    if not value:
        return []
    result: list[int] = []
    cursor = 0
    while True:
        found = text.find(value, cursor)
        if found < 0:
            return result
        result.append(found)
        cursor = found + 1


def _compare_rendered_layout(source_pdf: bytes, candidate_pdf: bytes,
                              replacements: list[tuple[str, str]], *, tolerance: float = 0.35) -> dict[str, Any]:
    """Compare rendered glyph layout outside uniquely mapped edited spans.

    Repeated target text is ambiguous without a renderer-provided source locator,
    so it blocks verification instead of normalizing every matching occurrence.
    All non-target text and its page and glyph bounding boxes must remain stable.
    """
    errors: list[str] = []
    old_layout, new_layout = _qpdfium_layout(source_pdf), _qpdfium_layout(candidate_pdf)
    if len(old_layout) != len(new_layout):
        errors.append("page_count_changed")
    old_text, old_glyphs = _flatten_layout(old_layout)
    new_text, new_glyphs = _flatten_layout(new_layout)
    old_spans: list[tuple[int, int]] = []
    new_spans: list[tuple[int, int]] = []
    for old_value, new_value in replacements:
        old_matches, new_matches = _occurrences(old_text, old_value), _occurrences(new_text, new_value)
        if len(old_matches) != 1 or len(new_matches) != 1:
            errors.append("ambiguous_target_render_match" if len(old_matches) > 1 or len(new_matches) > 1
                          else "target_text_missing_or_wrapped")
            continue
        old_start, new_start = old_matches[0], new_matches[0]
        old_end, new_end = old_start + len(old_value), new_start + len(new_value)
        if "\f" in old_text[old_start:old_end] or "\f" in new_text[new_start:new_end]:
            errors.append("target_crosses_page_boundary")
            continue
        old_spans.append((old_start, old_end))
        new_spans.append((new_start, new_end))
    if len(old_spans) != len(replacements) or len(new_spans) != len(replacements):
        return {"errors": sorted(set(errors)), "source_pages": len(old_layout),
                "candidate_pages": len(new_layout), "compared_unchanged_glyphs": 0}
    for spans in (old_spans, new_spans):
        ordered = sorted(spans)
        if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
            errors.append("overlapping_target_render_spans")
    if errors:
        return {"errors": sorted(set(errors)), "source_pages": len(old_layout),
                "candidate_pages": len(new_layout), "compared_unchanged_glyphs": 0}

    def unchanged(glyphs, spans):
        excluded = {index for start, end in spans for index in range(start, end)}
        return [(index, glyph) for index, glyph in enumerate(glyphs)
                if index not in excluded and glyph is not None]

    old_unchanged, new_unchanged = unchanged(old_glyphs, old_spans), unchanged(new_glyphs, new_spans)
    if len(old_unchanged) != len(new_unchanged) or any(
        old_glyph[1] != new_glyph[1]
        for (_, old_glyph), (_, new_glyph) in zip(old_unchanged, new_unchanged)
    ):
        errors.append("outside_content_changed")
    else:
        for (_, old_glyph), (_, new_glyph) in zip(old_unchanged, new_unchanged):
            old_page, _, old_box = old_glyph
            new_page, _, new_box = new_glyph
            if old_page != new_page:
                errors.append("outside_content_page_changed")
                break
            if old_glyph[1] in {"\r", "\n"}:
                # PDF text APIs anchor line separators at the preceding line's
                # final glyph, whose x position naturally changes with an edit.
                # The separator remains in the exact text comparison above.
                continue
            if old_box is None or new_box is None:
                if old_box != new_box:
                    errors.append("outside_content_geometry_unavailable")
                    break
            elif any(abs(before - after) > tolerance for before, after in zip(old_box, new_box)):
                errors.append("outside_content_position_changed")
                break
    if not errors:
        pixels = _compare_rendered_pixels(source_pdf, candidate_pdf, old_glyphs, new_glyphs,
                                           old_spans, new_spans)
        errors.extend(pixels["errors"])
    else:
        pixels = {"pixels_changed_outside_targets": None,
                  "pixels_compared_outside_targets": 0}
    return {"errors": sorted(set(errors)), "source_pages": len(old_layout),
            "candidate_pages": len(new_layout), "compared_unchanged_glyphs": min(len(old_unchanged), len(new_unchanged)),
            "position_tolerance_points": tolerance, **pixels}


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
            visual = _compare_rendered_layout(
                old_pdf, new_pdf,
                [(str(patch.expected_old_value.value), str(patch.new_value.value)) for patch in plan.patches],
            )
            results.append(CheckResult("visual", "PASS" if not visual["errors"] else "FAIL", True,
                                       {"renderer": "LibreOffice + PDFium glyph geometry", **visual}))
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
