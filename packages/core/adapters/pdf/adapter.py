from __future__ import annotations

import hashlib
import io
import json
import struct
from dataclasses import dataclass
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.constants import TrailerKeys as TK
from pypdf.generic import ArrayObject, NameObject, NumberObject, StreamObject
from pypdf.constants import FieldFlag

from packages.core.models.contracts import ChangePermission, Patch, PatchPlan, TypedValue


class PdfDocumentError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _QpdfConformantIncrementalWriter(PdfWriter):
    """pypdf incremental writer with the required xref-stream self entry.

    The pypdf releases currently supported by the application omit the xref
    stream's own type-1 entry. qpdf reports that omission as a structural
    warning. This pinned compatibility override retains pypdf's incremental
    object update algorithm and adds the missing entry to the appended xref
    stream; it never rewrites the original byte prefix.
    """

    def _write_increment(self, stream) -> None:  # pypdf private extension point, pinned and regression-tested
        object_positions: dict[int, int] = {}
        object_blocks: list[list[int]] = []
        current_start, current_stop = -1, -2
        original_hash_count = len(self._original_hash)
        for index, obj in enumerate(self._objects):
            if obj is None or (index < original_hash_count and obj.hash_bin() == self._original_hash[index]):
                continue
            object_id = index + 1
            object_positions[object_id] = stream.tell()
            stream.write(f"{object_id} 0 obj\n".encode())
            obj.write_to_stream(stream)
            stream.write(b"\nendobj\n")
            if object_id != current_stop:
                if current_start > 0:
                    object_blocks.append([current_start, current_stop - current_start])
                current_start = object_id
            current_stop = object_id + 1
        if current_start <= 0:
            raise PdfDocumentError("incremental_writer_has_no_changes")
        object_blocks.append([current_start, current_stop - current_start])

        xref_location = stream.tell()
        xref_object_id = len(self._objects) + 1
        stream.write(f"{xref_object_id} 0 obj\n".encode())
        # The xref stream is the highest new object number, so it is a separate
        # final /Index segment with its own offset entry.
        object_blocks.append([xref_object_id, 1])
        init_data = {
            NameObject("/Type"): NameObject("/XRef"),
            NameObject("/Size"): NumberObject(xref_object_id + 1),
            NameObject("/Root"): self.root_object.indirect_reference,
            NameObject("/Filter"): NameObject("/FlateDecode"),
            NameObject("/Index"): ArrayObject([NumberObject(item) for pair in object_blocks for item in pair]),
            NameObject("/W"): ArrayObject([NumberObject(1), NumberObject(4), NumberObject(1)]),
            "__streamdata__": b"",
        }
        if self._info is not None and (
            self._info.indirect_reference.idnum - 1 >= len(self._original_hash)
            or self._info.hash_bin() != self._original_hash[self._info.indirect_reference.idnum - 1]
        ):
            init_data[NameObject(TK.INFO)] = self._info.indirect_reference
        init_data[NameObject(TK.PREV)] = NumberObject(self._reader._startxref)
        if self._ID:
            init_data[NameObject(TK.ID)] = self._ID
        xref = StreamObject.initialize_from_dictionary(init_data)
        xref.set_data(b"".join(struct.pack(b">BIB", 1, offset, 0) for offset in object_positions.values())
                      + struct.pack(b">BIB", 1, xref_location, 0))
        xref.write_to_stream(stream)
        stream.write(b"\nendobj\n")
        stream.write(f"\nstartxref\n{xref_location}\n%%EOF\n".encode())


@dataclass(frozen=True)
class PdfTarget:
    id: str
    version_id: str
    sheet: str
    cell: str
    value: TypedValue
    fingerprint: str
    style_id: str | None = None
    editable: bool = True
    reason: str | None = None
    dependents: tuple[str, ...] | None = ()
    label: str | None = None
    field_type: str = "text"
    native_locator: dict[str, Any] | None = None
    options: tuple[str, ...] = ()
    max_length: int | None = None
    font_size: float | None = None


def _reader(source: bytes) -> PdfReader:
    try:
        reader = PdfReader(io.BytesIO(source), strict=True)
    except Exception as exc:
        raise PdfDocumentError("invalid_pdf") from exc
    if reader.is_encrypted:
        raise PdfDocumentError("encrypted_pdf")
    return reader


def _resolve(value: Any) -> Any:
    try:
        return value.get_object()
    except (AttributeError, TypeError):
        return value


def _object_identity(value: Any) -> tuple[Any, ...]:
    reference = getattr(value, "indirect_reference", None)
    if reference is not None:
        return (getattr(reference, "idnum", None), getattr(reference, "generation", None))
    return (id(value),)


def _field_fingerprint(name: str, field: Any, widgets: list[tuple[int, Any]]) -> str:
    value = {
        "name": name,
        "type": str(field.get("/FT", "")),
        "flags": int(field.get("/Ff", 0) or 0),
        "value": str(field.get("/V", "")),
        "maxlen": field.get("/MaxLen"),
        "options": [str(v) for v in field.get("/Opt", [])] if field.get("/Opt") else [],
        "widgets": [(page, [float(x) for x in widget.get("/Rect", [])]) for page, widget in widgets],
        "default_appearance": str(field.get("/DA", "")),
        "widget_appearances": [str(widget.get("/DA", "")) for _, widget in widgets],
        "appearance_states": [sorted(str(key) for key in _resolve(widget.get("/AP", {})).get("/N", {}).keys())
                              for _, widget in widgets if widget.get("/AP")],
    }
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()


class PdfAdapter:
    """AcroForm-only patcher. Existing page content is never reconstructed."""

    version = "0.1.0"

    def inspect(self, source: bytes):
        from packages.core.models.contracts import CapabilityProfile

        reader = _reader(source)
        root = reader.root_object
        acro = _resolve(root.get("/AcroForm", {}))
        fields = reader.get_fields() or {}
        reasons: list[str] = []
        features: list[str] = ["digital_pdf" if any(p.extract_text() for p in reader.pages) or fields else "scanned_or_image_pdf"]
        if acro.get("/XFA"):
            reasons.append("xfa_form")
        if root.get("/Perms") and _resolve(root["/Perms"]).get("/DocMDP"):
            reasons.append("certified_pdf")
        for field in fields.values():
            if str(field.get("/FT", "")) == "/Sig" and field.get("/V"):
                reasons.append("signed_pdf")
        editable_count = sum(1 for target in self.targets(source) if target.editable)
        if not fields:
            reasons.append("no_acroform_fields")
        level = "editable" if editable_count and not reasons else "read_only"
        return CapabilityProfile(level, tuple(dict.fromkeys(reasons)), tuple(features), (), "pdf", self.version)

    def targets(self, source: bytes) -> list[PdfTarget]:
        reader = _reader(source)
        root = reader.root_object
        acro = _resolve(root.get("/AcroForm", {}))
        fields = reader.get_fields() or {}
        global_reason = None
        if acro.get("/XFA"):
            global_reason = "xfa_form"
        if root.get("/Perms") and _resolve(root["/Perms"]).get("/DocMDP"):
            global_reason = "certified_pdf"
        if any(str(field.get("/FT", "")) == "/Sig" and field.get("/V") for field in fields.values()):
            global_reason = "signed_pdf"
        widgets_by_name: dict[str, list[tuple[int, Any]]] = {}
        for page_no, page in enumerate(reader.pages, start=1):
            for ref in page.get("/Annots", []):
                widget = _resolve(ref)
                if str(widget.get("/Subtype", "")) != "/Widget":
                    continue
                name = widget.get("/T")
                parent = _resolve(widget.get("/Parent")) if widget.get("/Parent") else None
                pieces = [str(name)] if name is not None else []
                current = parent
                while current:
                    if current.get("/T") is not None:
                        pieces.insert(0, str(current["/T"]))
                    current = _resolve(current.get("/Parent")) if current.get("/Parent") else None
                full_name = ".".join(pieces)
                if full_name in fields:
                    widgets_by_name.setdefault(full_name, []).append((page_no, widget))
        calculation_fields = {_object_identity(_resolve(ref)) for ref in acro.get("/CO", [])} if acro.get("/CO") else set()
        result: list[PdfTarget] = []
        for name, field in fields.items():
            if not isinstance(field, dict):
                continue
            field_obj = _resolve(field)
            widgets = widgets_by_name.get(name, [])
            ft = str(field_obj.get("/FT", ""))
            flags = int(field_obj.get("/Ff", 0) or 0)
            reason: str | None = global_reason
            kind = "text"
            value: TypedValue
            options: tuple[str, ...] = ()
            if ft == "/Tx":
                raw = field_obj.get("/V", "")
                value = TypedValue("string", str(raw))
                if flags & int(FieldFlag.READ_ONLY):
                    reason = "read_only_field"
                if field_obj.get("/AA") or field_obj.get("/A") or _object_identity(field_obj) in calculation_fields:
                    reason = "calculation_or_field_action"
            elif ft == "/Btn":
                is_radio = bool(flags & (1 << 15))
                kind = "radio" if is_radio else "checkbox"
                raw = str(field_obj.get("/V", "/Off"))
                if is_radio:
                    value = TypedValue("choice", "" if raw == "/Off" else raw.lstrip("/"))
                else:
                    value = TypedValue("boolean", raw != "/Off")
                state_values: set[str] = set()
                for _, widget in widgets:
                    appearance = _resolve(widget.get("/AP", {}))
                    normal = _resolve(appearance.get("/N", {})) if appearance else {}
                    state_values.update(str(key).lstrip("/") for key in normal.keys() if str(key) != "/Off")
                # Checkbox state names are PDF appearance internals; exposing
                # them as selectable values creates a broken user control.
                # Preserve the sole on-state in the native locator and render
                # a boolean control. Radio groups remain named choices.
                options = tuple(sorted(state_values)) if is_radio else ()
                if is_radio and not options:
                    reason = "unsupported_radio_appearance"
                if not is_radio and len(state_values) != 1:
                    reason = "unsupported_checkbox_appearance"
                if flags & int(FieldFlag.READ_ONLY):
                    reason = "read_only_field"
            elif ft == "/Ch":
                kind = "choice"
                raw = field_obj.get("/V", "")
                value = TypedValue("choice", str(raw))
                options = tuple(str(opt[0] if isinstance(opt, (list, tuple)) else opt)
                                for opt in field_obj.get("/Opt", []))
                if not options:
                    reason = "unsupported_choice_options"
                if flags & int(FieldFlag.READ_ONLY):
                    reason = "read_only_field"
            else:
                continue
            if str(field_obj.get("/FT", "")) == "/Sig":
                continue
            if field_obj.get("/AA") or field_obj.get("/A") or _object_identity(field_obj) in calculation_fields:
                reason = "calculation_or_field_action"
            widget_list = widgets
            if not widget_list:
                reason = reason or "widget_not_found"
            elif any(not _has_normal_appearance(widget) for _, widget in widget_list):
                reason = reason or "unsupported_appearance"
            if kind in {"text", "choice"}:
                for _, widget in widget_list:
                    reason = reason or _appearance_chrome_reason(widget)
            page = widget_list[0][0] if widget_list else 0
            rect = [float(x) for x in widget_list[0][1].get("/Rect", [])] if widget_list else []
            da = str((widget_list[0][1].get("/DA") if widget_list else None) or field_obj.get("/DA") or acro.get("/DA", ""))
            font_match = __import__("re").search(r"/(\S+)\s+([0-9]+(?:\.[0-9]+)?)\s+Tf", da)
            font_size = float(font_match.group(2)) if font_match else None
            if kind in {"text", "choice"}:
                if any(not _supported_default_appearance(
                        str((widget.get("/DA") or field_obj.get("/DA") or acro.get("/DA", ""))))
                       for _, widget in widget_list):
                    reason = reason or "unsupported_default_appearance"
                font_resources = _resolve(_resolve(acro.get("/DR", {})).get("/Font", {}))
                font = _resolve(font_resources.get("/" + font_match.group(1))) if font_match and font_resources else None
                standard_fonts = {"/Courier", "/Courier-Bold", "/Courier-Oblique", "/Courier-BoldOblique",
                                  "/Helvetica", "/Helvetica-Bold", "/Helvetica-Oblique", "/Helvetica-BoldOblique",
                                  "/Times-Roman", "/Times-Bold", "/Times-Italic", "/Times-BoldItalic",
                                  "/Symbol", "/ZapfDingbats"}
                if font_size is None or font_size <= 0 or font is None or str(font.get("/BaseFont", "")) not in standard_fonts:
                    reason = reason or "ambiguous_appearance"
            maxlen = (widget_list[0][1].get("/MaxLen") if widget_list else None) or field_obj.get("/MaxLen")
            maxlen = int(maxlen) if maxlen is not None else None
            fingerprint = _field_fingerprint(name, field_obj, widget_list)
            locator = {"field_name": name, "field_type": kind, "pages": [p for p, _ in widget_list],
                       "widgets": [{"page": p, "rect": [float(x) for x in w.get("/Rect", [])]} for p, w in widget_list]}
            if kind == "checkbox" and len(state_values) == 1:
                locator["on_state"] = next(iter(state_values))
            result.append(PdfTarget(f"pdf:{name}", "", name, str(page), value, fingerprint,
                                    editable=reason is None, reason=reason, label=name, field_type=kind,
                                    native_locator=locator, options=options, max_length=maxlen, font_size=font_size))
        return result

    def unsupported_targets(self, source: bytes) -> list[PdfTarget]:
        return [target for target in self.targets(source) if not target.editable]

    def formula_dependents(self, source: bytes, target: PdfTarget):
        return (), False

    def type_compatible(self, old: TypedValue, new: TypedValue) -> bool:
        return old.type == new.type

    def validate_new_value(self, value: TypedValue) -> None:
        if value.type not in {"string", "boolean", "choice"}:
            raise PdfDocumentError("pdf_field_type_mismatch")

    def permission_for(self, source: bytes, target: PdfTarget, new: TypedValue, provenance: dict[str, str]):
        expected = hashlib.sha256(json.dumps(new.canonical(), sort_keys=True).encode()).hexdigest()
        return [ChangePermission("pdf", target.id, "replace_value", target.fingerprint, expected)]

    def preflight(self, source: bytes, target: PdfTarget, patch: Patch) -> None:
        if hashlib.sha256(source).hexdigest() != patch.source_version_id:
            raise PdfDocumentError("stale_source")
        fresh = next((t for t in self.targets(source) if t.id == patch.target_id), None)
        if fresh is None or not fresh.editable:
            raise PdfDocumentError(fresh.reason if fresh else "target_not_found")
        if fresh.fingerprint != patch.expected_fingerprint:
            raise PdfDocumentError("fingerprint_mismatch")
        if fresh.value.type != patch.expected_old_value.type or fresh.value.value != patch.expected_old_value.value:
            raise PdfDocumentError("expected_value_mismatch")
        if not self.type_compatible(fresh.value, patch.new_value):
            raise PdfDocumentError("pdf_field_type_mismatch")
        if fresh.field_type == "text":
            text = str(patch.new_value.value)
            if fresh.max_length is not None and len(text) > fresh.max_length:
                raise PdfDocumentError("max_length_exceeded")
            widget = (fresh.native_locator or {}).get("widgets", [{}])[0].get("rect", [])
            width = abs(widget[2] - widget[0]) if len(widget) == 4 else 0
            # Conservative estimate: unsupported if the selected font is ambiguous, and
            # reject overflows rather than shrinking/reflowing the field implicitly.
            if not text.isascii() or any(ord(char) < 32 for char in text):
                raise PdfDocumentError("unsupported_glyph")
            if not fresh.font_size or width <= 0 or len(text) * fresh.font_size > width - 4:
                raise PdfDocumentError("widget_overflow")
        if fresh.field_type == "checkbox" and not isinstance(patch.new_value.value, bool):
            raise PdfDocumentError("invalid_checkbox_value")
        if fresh.field_type in {"choice", "radio"} and str(patch.new_value.value) not in fresh.options:
            raise PdfDocumentError("invalid_choice")

    def apply(self, source: bytes, plan: PatchPlan) -> bytes:
        if not plan.patches or plan.no_change:
            raise PdfDocumentError("no_change_has_no_candidate")
        reader = _reader(source)
        targets = {t.id: t for t in self.targets(source)}
        values: dict[str, Any] = {}
        button_values: dict[str, tuple[str, str]] = {}
        for patch in plan.patches:
            target = targets.get(patch.target_id)
            if target is None:
                raise PdfDocumentError("target_not_found")
            self.preflight(source, target, patch)
            name = (target.native_locator or {})["field_name"]
            if target.field_type == "checkbox":
                # Resolve the widget's actual on-state name instead of assuming /Yes.
                on_state = (target.native_locator or {}).get("on_state")
                if not on_state:
                    raise PdfDocumentError("unsupported_checkbox_appearance")
                button_values[name] = (target.field_type, on_state if patch.new_value.value else "Off")
            elif target.field_type == "radio":
                button_values[name] = (target.field_type, str(patch.new_value.value).lstrip("/"))
            else:
                values[name] = str(patch.new_value.value)
        writer = _QpdfConformantIncrementalWriter(reader, incremental=True)
        source_acro = _resolve(reader.root_object.get("/AcroForm", {}))
        had_need_appearances = "/NeedAppearances" in source_acro
        need_appearances = source_acro.get("/NeedAppearances")
        if values:
            writer.update_page_form_field_values(None, values, auto_regenerate=False)
        # pypdf currently maps auto_regenerate=False to /NeedAppearances false.
        # Restore the source entry exactly (including absence) so the flag never
        # changes and cannot trigger a viewer-wide appearance regeneration.
        writer_acro = _resolve(writer._root_object.get("/AcroForm", {}))
        need_appearances_key = NameObject("/NeedAppearances")
        if had_need_appearances:
            writer_acro[need_appearances_key] = need_appearances
        else:
            writer_acro.pop(need_appearances_key, None)
        for name, (field_type, selected_value) in button_values.items():
            field = _writer_field(writer, name)
            widgets = _writer_widgets(writer, name)
            if field is None or not widgets:
                raise PdfDocumentError("button_widgets_not_found")
            selected_state = NameObject("/Off" if selected_value == "Off" else f"/{selected_value}")
            field[NameObject("/V")] = selected_state
            selected_widgets = 0
            for widget in widgets:
                appearance = _resolve(widget.get("/AP", {}))
                normal = _resolve(appearance.get("/N", {})) if appearance else {}
                state_names = {str(value) for value in normal.keys()}
                if field_type == "radio":
                    widget_selected = str(selected_state) in state_names
                else:
                    widget_selected = selected_value != "Off" and str(selected_state) in state_names
                widget[NameObject("/AS")] = selected_state if widget_selected else NameObject("/Off")
                selected_widgets += int(widget_selected)
            if selected_value != "Off" and not selected_widgets:
                raise PdfDocumentError("selected_button_appearance_missing")
        for patch in plan.patches:
            target = targets[patch.target_id]
            if target.field_type not in {"text", "choice"}:
                continue
            name = (target.native_locator or {})["field_name"]
            for widget in _writer_widgets(writer, name):
                _restore_widget_chrome(widget)
        provenance = plan.provenance_dict()
        writer.add_metadata({
            "/Channel360_Source_SHA256": plan.source_sha256,
            "/Channel360_Edit_Set": provenance.get("edit_set_id", ""),
            "/Channel360_Patch_Plan_SHA256": plan.plan_sha256,
        })
        output = io.BytesIO()
        writer.write(output)
        result = output.getvalue()
        if not result.startswith(source):
            raise PdfDocumentError("incremental_prefix_not_preserved")
        return result


def _has_normal_appearance(widget: Any) -> bool:
    appearances = _resolve(widget.get("/AP", {}))
    if not appearances:
        return False
    normal = _resolve(appearances.get("/N"))
    return normal is not None


def _appearance_chrome_reason(widget: Any) -> str | None:
    mk = _resolve(widget.get("/MK", {}))
    for key in ("/BG", "/BC"):
        if key in mk and _color_operator(mk[key], fill=(key == "/BG")) is None:
            return "unsupported_widget_appearance_color"
    if "/BC" in mk:
        border_style = _resolve(widget.get("/BS", {}))
        if border_style.get("/S", "/S") != "/S":
            return "unsupported_widget_border_style"
        border = widget.get("/Border")
        if border is not None and len(_resolve(border)) > 3 and _resolve(border)[3]:
            return "unsupported_widget_border_style"
        try:
            border_width = float(border_style.get("/W", 1))
            old_border = _resolve(border) if border is not None else []
            if "/W" not in border_style and len(old_border) > 2:
                border_width = float(old_border[2])
        except (TypeError, ValueError):
            return "unsupported_widget_border_width"
        if border_width < 0:
            return "unsupported_widget_border_width"
    return None


def _supported_default_appearance(appearance: str) -> bool:
    import re

    font = re.search(r"/[A-Za-z0-9_.+-]+\s+[0-9]+(?:\.[0-9]+)?\s+Tf", appearance)
    if font is None:
        return False
    remaining = appearance[:font.start()] + appearance[font.end():]
    number = r"[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?"
    colors = re.compile(rf"(?:{number}\s+[gG]|{number}(?:\s+{number}){{2}}\s+[rR][gG]|"
                        rf"{number}(?:\s+{number}){{3}}\s+[kK])\b")
    return not colors.sub("", remaining).strip()


def _color_operator(value: Any, *, fill: bool) -> bytes | None:
    color = _resolve(value)
    try:
        components = [float(item) for item in color]
    except (TypeError, ValueError):
        return None
    if len(components) not in {1, 3, 4} or any(not 0 <= item <= 1 for item in components):
        return None
    suffix = {(1, True): "g", (3, True): "rg", (4, True): "k",
              (1, False): "G", (3, False): "RG", (4, False): "K"}[(len(components), fill)]
    return (" ".join(f"{item:g}" for item in components) + f" {suffix}\n").encode("ascii")


def _restore_widget_chrome(widget: Any) -> None:
    mk = _resolve(widget.get("/MK", {}))
    normal = _resolve(_resolve(widget["/AP"]).get("/N"))
    if not hasattr(normal, "get_data") or not hasattr(normal, "set_data"):
        raise PdfDocumentError("unsupported_widget_appearance_stream")
    rect = [float(item) for item in widget.get("/Rect", [])]
    if len(rect) != 4:
        raise PdfDocumentError("unsupported_widget_rectangle")
    width, height = abs(rect[2] - rect[0]), abs(rect[3] - rect[1])
    background = _color_operator(mk["/BG"], fill=True) if "/BG" in mk else None
    border = _color_operator(mk["/BC"], fill=False) if "/BC" in mk else None
    before = bytearray(b"q\n")
    if background is not None:
        before.extend(background)
        before.extend(f"0 0 {width:g} {height:g} re f\n".encode("ascii"))
    before.extend(b"Q\n")
    after = bytearray()
    if border is not None:
        border_style = _resolve(widget.get("/BS", {}))
        border_width = float(border_style.get("/W", 1))
        old_border = _resolve(widget.get("/Border", []))
        if "/W" not in border_style and len(old_border) > 2:
            border_width = float(old_border[2])
        if border_width > 0:
            inset = border_width / 2
            after.extend(b"q\n")
            after.extend(border)
            after.extend(f"{border_width:g} w\n{inset:g} {inset:g} "
                         f"{max(0, width - border_width):g} {max(0, height - border_width):g} re S\nQ\n".encode("ascii"))
    normal.set_data(bytes(before) + normal.get_data() + bytes(after))


def _writer_widgets(writer: PdfWriter, name: str) -> list[Any]:
    found = []
    for page in writer.pages:
        for reference in page.get("/Annots", []):
            widget = _resolve(reference)
            if str(widget.get("/Subtype", "")) != "/Widget":
                continue
            pieces = [str(widget["/T"])] if widget.get("/T") is not None else []
            parent = widget.get("/Parent")
            while parent is not None:
                parent = _resolve(parent)
                if parent.get("/T") is not None:
                    pieces.insert(0, str(parent["/T"]))
                parent = parent.get("/Parent")
            if ".".join(pieces) == name:
                found.append(widget)
    return found


def _writer_field(writer: PdfWriter, name: str) -> Any | None:
    fields = _resolve(_resolve(writer._root_object.get("/AcroForm", {})).get("/Fields", []))

    def find(items, prefix=""):
        for reference in items:
            field = _resolve(reference)
            partial = str(field.get("/T", ""))
            qualified = f"{prefix}.{partial}" if prefix and partial else partial or prefix
            if qualified == name:
                return field
            child = _resolve(field.get("/Kids", []))
            found = find(child, qualified) if child else None
            if found is not None:
                return found
        return None

    return find(fields)
