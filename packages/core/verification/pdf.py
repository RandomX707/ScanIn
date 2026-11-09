from __future__ import annotations

import hashlib
import io
import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw
from pypdf import PdfReader
from pypdf.generic import NameObject

from packages.core.models.contracts import CheckResult, PatchPlan, VerifyReport


def _blocked(source_hash: str, output_hash: str, reason: str) -> VerifyReport:
    return VerifyReport((CheckResult("integrity", "FAIL", True, {"reason": reason}),),
                        "BLOCKED", source_hash, output_hash)


def _frozen_plan_hash(plan: PatchPlan) -> str:
    if not plan.patches:
        body = {"source_sha256": plan.source_sha256, "patches": [], "provenance": plan.provenance}
    else:
        from dataclasses import asdict
        body = {
            "source_sha256": plan.source_sha256,
            "patches": [{"source_version_id": p.source_version_id, "target_id": p.target_id,
                         "expected_old_value": p.expected_old_value.canonical(),
                         "expected_fingerprint": p.expected_fingerprint, "new_value": p.new_value.canonical()}
                        for p in plan.patches],
            "permissions": [asdict(p) for p in plan.permitted_changes],
            "provenance": plan.provenance, "dependents": list(plan.dependents),
            "dependencies_unknown": plan.dependencies_unknown, "no_change": plan.no_change,
        }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _read_fields(data: bytes) -> tuple[PdfReader, dict[str, dict[str, Any]]]:
    reader = PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted:
        raise ValueError("encrypted_pdf")
    fields = reader.get_fields() or {}
    return reader, {name: dict(field) for name, field in fields.items()}


def _resolve(value: Any) -> Any:
    try:
        return value.get_object()
    except (AttributeError, TypeError):
        return value


def _canonical_pdf_object(value: Any) -> Any:
    if hasattr(value, "get_object"):
        value = value.get_object()
    if isinstance(value, dict):
        result = {str(k): _canonical_pdf_object(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))
                  if str(k) not in {"/P", "/Parent"}}
        if hasattr(value, "get_data"):
            result["__streamdata__"] = value.get_data().hex()
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_pdf_object(item) for item in value]
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _page_content(page) -> bytes:
    contents = page.get_contents()
    return contents.get_data() if contents is not None else b""


def _widget_image_rect(page, rect: list[float], image_size: tuple[int, int]) -> tuple[int, int, int, int]:
    """Map PDF user-space widget coordinates through CropBox and /Rotate."""
    crop = page.cropbox
    left, bottom = float(crop.left), float(crop.bottom)
    right, top = float(crop.right), float(crop.top)
    width, height = right - left, top - bottom
    rotation = int(page.get("/Rotate", 0) or 0) % 360
    if rotation not in {0, 90, 180, 270}:
        raise ValueError("unsupported_page_rotation")
    display_width, display_height = (height, width) if rotation in {90, 270} else (width, height)
    scale_x, scale_y = image_size[0] / display_width, image_size[1] / display_height
    x0, y0, x1, y1 = rect
    points = [(x0, y0), (x0, y1), (x1, y0), (x1, y1)]
    mapped = []
    for x, y in points:
        if rotation == 0:
            px, py = x - left, top - y
        elif rotation == 90:
            px, py = y - bottom, x - left
        elif rotation == 180:
            px, py = right - x, y - bottom
        else:  # 270 degrees clockwise
            px, py = top - y, right - x
        mapped.append((px * scale_x, py * scale_y))
    return (max(0, math.floor(min(point[0] for point in mapped)) - 1),
            max(0, math.floor(min(point[1] for point in mapped)) - 1),
            min(image_size[0] - 1, math.ceil(max(point[0] for point in mapped)) + 1),
            min(image_size[1] - 1, math.ceil(max(point[1] for point in mapped)) + 1))


def _render(data: bytes, scale: float = 1.0, *, forms_enabled: bool = True) -> list[Image.Image]:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(data)
    try:
        if forms_enabled:
            document.init_forms()
        result: list[Image.Image] = []
        for page in document:
            result.append(page.render(scale=scale, may_draw_forms=forms_enabled,
                                      rev_byteorder=True).to_pil().convert("RGB"))
        return result
    finally:
        document.close()


class PdfIndependentVerifier:
    """Critical PDF verifier: qpdf, structural preservation and PDFium pixel comparison."""

    def verify(self, source: bytes, candidate: bytes, plan: PatchPlan) -> VerifyReport:
        source_hash, output_hash = hashlib.sha256(source).hexdigest(), hashlib.sha256(candidate).hexdigest()
        if (_frozen_plan_hash(plan) != plan.plan_sha256 or plan.source_sha256 != source_hash
                or plan.source_version_id != source_hash or plan.no_change or not plan.patches):
            return _blocked(source_hash, output_hash, "frozen_plan_or_source_mismatch")

        if not candidate.startswith(source):
            return _blocked(source_hash, output_hash, "original_byte_prefix_not_preserved")

        qpdf = shutil.which("qpdf")
        if not qpdf:
            return _blocked(source_hash, output_hash, "qpdf_unavailable")
        qpdf = str(Path(qpdf).resolve())
        try:
            with tempfile.TemporaryDirectory(prefix="channel360-pdf-verify-") as temp_dir:
                path = Path(temp_dir) / "candidate.pdf"
                path.write_bytes(candidate)
                qcheck = subprocess.run([qpdf, "--check", str(path)], capture_output=True, timeout=15, check=False)
            if qcheck.returncode != 0:
                return _blocked(source_hash, output_hash, "qpdf_check_failed")
        except (OSError, subprocess.TimeoutExpired) as exc:
            suffix = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else type(exc).__name__.lower()
            return _blocked(source_hash, output_hash, f"qpdf_check_unavailable:{suffix}")

        try:
            old_reader, old_fields = _read_fields(source)
            new_reader, new_fields = _read_fields(candidate)
            if len(old_reader.pages) != len(new_reader.pages):
                return _blocked(source_hash, output_hash, "page_count_changed")
            if old_fields.keys() != new_fields.keys():
                return _blocked(source_hash, output_hash, "field_set_changed")
        except Exception:
            return _blocked(source_hash, output_hash, "independent_pdf_parse_failed")

        target_names = {p.target_id.removeprefix("pdf:") for p in plan.patches}
        allowed = {(p.part, p.locator, p.operation) for p in plan.permitted_changes}
        permission_ok = (len(target_names) == len(plan.patches)
                         and allowed == {("pdf", p.target_id, "replace_value") for p in plan.patches})
        value_errors: list[str] = []
        for patch in plan.patches:
            name = patch.target_id.removeprefix("pdf:")
            field = new_fields.get(name)
            old = old_fields.get(name)
            actual_value = str(field.get("/V", "")) if field is not None else ""
            is_button = field is not None and str(field.get("/FT", "")) == "/Btn"
            if is_button and not isinstance(field.get("/V"), NameObject):
                value_errors.append(f"{patch.target_id}:button_value_not_name")
            matches = (actual_value != "/Off") if patch.new_value.type == "boolean" and patch.new_value.value else (
                actual_value == "/Off" if patch.new_value.type == "boolean" else
                actual_value.lstrip("/") == str(patch.new_value.value) if field and str(field.get("/FT")) == "/Btn" else
                actual_value == str(patch.new_value.value))
            if field is None or old is None or not matches:
                value_errors.append(f"{patch.target_id}:readback_mismatch")
            widgets = _target_widget_objects(new_reader, name)
            if not widgets or any(not _has_normal_appearance(widget) for widget in widgets):
                value_errors.append(f"{patch.target_id}:appearance_stream_missing")
            if field is not None and str(field.get("/FT", "")) == "/Btn":
                old_widgets = _target_widget_objects(old_reader, name)
                widgets = _target_widget_objects(new_reader, name)
                flags = int(field.get("/Ff", 0) or 0)
                is_radio = bool(flags & (1 << 15))
                if is_radio:
                    selected_state = "/Off" if str(patch.new_value.value) in {"", "/Off"} else f"/{str(patch.new_value.value).lstrip('/')}"
                elif patch.new_value.type == "boolean" and not patch.new_value.value:
                    selected_state = "/Off"
                else:
                    states = {str(key) for widget in old_widgets
                              for key in _normal_appearance_states(widget) if str(key) != "/Off"}
                    if len(states) != 1:
                        value_errors.append(f"{patch.target_id}:checkbox_appearance_state_ambiguous")
                    selected_state = next(iter(states), "/Off")
                seen_selected_state = False
                for widget in widgets:
                    normal = _normal_appearance_states(widget)
                    actual_state = str(widget.get("/AS", "/Off"))
                    should_select = selected_state != "/Off" and selected_state in {str(key) for key in normal}
                    expected_state = selected_state if should_select else "/Off"
                    seen_selected_state |= should_select
                    if actual_state != expected_state:
                        value_errors.append(f"{patch.target_id}:button_appearance_state_mismatch")
                    if actual_state not in {str(key) for key in normal}:
                        value_errors.append(f"{patch.target_id}:button_appearance_state_missing")
                if selected_state != "/Off" and not seen_selected_state:
                    value_errors.append(f"{patch.target_id}:button_appearance_state_missing")
        unintended = [name for name in old_fields if name not in target_names
                      and _canonical_pdf_object(old_fields[name]) != _canonical_pdf_object(new_fields[name])]
        for name in target_names & old_fields.keys():
            for key in ("/FT", "/Ff", "/MaxLen", "/Opt", "/DV"):
                if _canonical_pdf_object(old_fields[name].get(key)) != _canonical_pdf_object(new_fields[name].get(key)):
                    value_errors.append(f"pdf:{name}:{key}:changed")
        if any(_page_content(old_reader.pages[i]) != _page_content(new_reader.pages[i]) for i in range(len(old_reader.pages))):
            unintended.append("page_content_changed")

        # Require exact non-target annotation preservation and unchanged page-level annotations.
        annotation_errors: list[str] = []
        for page_index, (old_page, new_page) in enumerate(zip(old_reader.pages, new_reader.pages)):
            old_annots = _canonical_annotations(old_page, target_names)
            new_annots = _canonical_annotations(new_page, target_names)
            if len(old_annots) != len(new_annots):
                annotation_errors.append(f"page_{page_index + 1}:annotation_count_changed")
            # Widget appearance/value object updates are permitted only on target fields.
            # Stable annotation geometry/order and all non-widget annotations must remain.
            if old_annots != new_annots:
                annotation_errors.append(f"page_{page_index + 1}:unrelated_annotation_changed")
        old_acro = _resolve(old_reader.root_object.get("/AcroForm", {}))
        new_acro = _resolve(new_reader.root_object.get("/AcroForm", {}))
        if ("/NeedAppearances" in old_acro) != ("/NeedAppearances" in new_acro) or (
                _canonical_pdf_object(old_acro.get("/NeedAppearances")) !=
                _canonical_pdf_object(new_acro.get("/NeedAppearances"))):
            annotation_errors.append("acroform:/NeedAppearances:changed")
        annotation_errors.extend(_widget_appearance_errors(old_reader, new_reader, target_names))

        try:
            before, after = _render(source, forms_enabled=True), _render(candidate, forms_enabled=True)
            if len(before) != len(after):
                raise ValueError("render_page_count_changed")
            visual_errors: list[str] = []
            widgets_before = _render(source, forms_enabled=False)
            widgets_after = _render(candidate, forms_enabled=False)
            for page_index, (old_img, new_img) in enumerate(zip(before, after)):
                if old_img.size != new_img.size:
                    visual_errors.append(f"page_{page_index + 1}:render_size_changed")
                    continue
                if (_page_has_widgets(old_reader.pages[page_index]) or
                        _page_has_widgets(new_reader.pages[page_index])) and (
                        ImageChops.difference(old_img, widgets_before[page_index]).getbbox() is None or
                        ImageChops.difference(new_img, widgets_after[page_index]).getbbox() is None):
                    visual_errors.append(f"page_{page_index + 1}:form_widgets_not_rendered")
                diff = ImageChops.difference(old_img, new_img).convert("L")
                mask = Image.new("L", old_img.size, 255)
                draw = ImageDraw.Draw(mask)
                for patch in plan.patches:
                    for widget in _target_widgets(old_reader, patch.target_id.removeprefix("pdf:")):
                        if widget["page"] != page_index + 1:
                            continue
                        draw.rectangle(_widget_image_rect(old_reader.pages[page_index], widget["rect"], old_img.size),
                                       fill=0)
                diff = ImageChops.multiply(diff, mask)
                if diff.getbbox() is not None:
                    visual_errors.append(f"page_{page_index + 1}:pixels_changed_outside_target_widgets")
        except Exception as exc:
            return _blocked(source_hash, output_hash, f"critical_visual_check_unavailable:{type(exc).__name__}")

        results = [
            CheckResult("integrity", "PASS", True, {"qpdf_check": "PASS", "original_byte_prefix": True,
                                                      "page_count": len(old_reader.pages)}),
            CheckResult("policy", "PASS" if permission_ok else "FAIL", True,
                        {"target_permissions_match": permission_ok}),
            CheckResult("intent", "PASS" if not value_errors else "FAIL", True, {"errors": value_errors}),
            CheckResult("preservation", "PASS" if not unintended and not annotation_errors else "FAIL", True,
                        {"unintended_changes": unintended, "annotation_errors": annotation_errors}),
            CheckResult("provenance", "PASS" if _provenance_matches(new_reader, plan) else "FAIL", True,
                        {"metadata_matches_frozen_plan": _provenance_matches(new_reader, plan)}),
            CheckResult("visual", "PASS" if not visual_errors else "FAIL", True,
                        {"renderer": "pypdfium2", "checked_every_page": True, "errors": visual_errors}),
        ]
        outcome = "VERIFIED" if all(item.status == "PASS" for item in results) else "BLOCKED"
        return VerifyReport(tuple(results), outcome, source_hash, output_hash)


def _target_widgets(reader: PdfReader, name: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for page_number, page in enumerate(reader.pages, start=1):
        for reference in page.get("/Annots", []):
            widget = reference.get_object()
            if str(widget.get("/Subtype", "")) != "/Widget":
                continue
            pieces = [str(widget["/T"])] if widget.get("/T") is not None else []
            parent = widget.get("/Parent")
            while parent is not None:
                parent = parent.get_object()
                if parent.get("/T") is not None:
                    pieces.insert(0, str(parent["/T"]))
                parent = parent.get("/Parent")
            if ".".join(pieces) == name:
                found.append({"page": page_number, "rect": [float(x) for x in widget.get("/Rect", [])]})
    return found


def _target_widget_objects(reader: PdfReader, name: str) -> list[Any]:
    found: list[Any] = []
    for page in reader.pages:
        for reference in page.get("/Annots", []):
            widget = reference.get_object()
            if str(widget.get("/Subtype", "")) == "/Widget" and _widget_field_name(widget) == name:
                found.append(widget)
    return found


def _has_normal_appearance(widget: Any) -> bool:
    appearance = _resolve(widget.get("/AP", {}))
    return bool(appearance and _resolve(appearance.get("/N")))


def _canonical_annotations(page, target_names: set[str]) -> list[Any]:
    output = []
    for reference in page.get("/Annots", []):
        annotation = reference.get_object()
        field_name = _widget_field_name(annotation) if str(annotation.get("/Subtype", "")) == "/Widget" else None
        normalized = _canonical_pdf_object(annotation)
        if field_name in target_names:
            normalized = {key: value for key, value in normalized.items() if key not in {"/AP", "/V", "/AS", "/I"}}
        output.append(normalized)
    return output


def _widget_field_name(widget) -> str:
    pieces = [str(widget["/T"])] if widget.get("/T") is not None else []
    parent = widget.get("/Parent")
    while parent is not None:
        parent = parent.get_object()
        if parent.get("/T") is not None:
            pieces.insert(0, str(parent["/T"]))
        parent = parent.get("/Parent")
    return ".".join(pieces)


def _page_has_widgets(page) -> bool:
    return any(str(reference.get_object().get("/Subtype", "")) == "/Widget"
               for reference in page.get("/Annots", []))


def _normal_appearance_states(widget) -> dict:
    appearance = _resolve(widget.get("/AP", {}))
    normal = _resolve(appearance.get("/N", {})) if appearance else {}
    return normal if hasattr(normal, "keys") else {}


def _widget_appearance_errors(old_reader: PdfReader, new_reader: PdfReader,
                              target_names: set[str]) -> list[str]:
    errors: list[str] = []
    for page_index, (old_page, new_page) in enumerate(zip(old_reader.pages, new_reader.pages), start=1):
        old_annots = list(old_page.get("/Annots", []))
        new_annots = list(new_page.get("/Annots", []))
        for old_ref, new_ref in zip(old_annots, new_annots):
            old_widget, new_widget = _resolve(old_ref), _resolve(new_ref)
            if (str(old_widget.get("/Subtype", "")) != "/Widget" or
                    str(new_widget.get("/Subtype", "")) != "/Widget"):
                continue
            name = _widget_field_name(old_widget)
            if name != _widget_field_name(new_widget):
                continue
            target = name in target_names
            for key in ("/AP", "/MK", "/DA", "/V"):
                if target and key in {"/AP", "/V"}:
                    continue
                if _canonical_pdf_object(old_widget.get(key)) != _canonical_pdf_object(new_widget.get(key)):
                    errors.append(f"page_{page_index}:pdf:{name}:{key}:changed")
    return errors


def _provenance_matches(reader: PdfReader, plan: PatchPlan) -> bool:
    metadata = reader.metadata or {}
    provenance = plan.provenance_dict()
    return (str(metadata.get("/Channel360_Source_SHA256", "")) == plan.source_sha256
            and str(metadata.get("/Channel360_Edit_Set", "")) == provenance.get("edit_set_id", "")
            and str(metadata.get("/Channel360_Patch_Plan_SHA256", "")) == plan.plan_sha256)
