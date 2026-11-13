from __future__ import annotations

import hashlib
import io
import json
import posixpath
import re
import zipfile
import xml.etree.ElementTree as StdET
from dataclasses import dataclass
from typing import Any

from lxml import etree

from packages.core.models.contracts import ChangePermission, Patch, PatchPlan, TypedValue

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML = "http://www.w3.org/XML/1998/namespace"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CUSTOM = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
VT = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
CUSTOM_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties"
CUSTOM_TYPE = "application/vnd.openxmlformats-officedocument.custom-properties+xml"
NS = {"w": W, "ct": CT, "rel": REL, "cp": CUSTOM}
PROVENANCE = {
    "Channel360_Source_SHA256": "source_sha256",
    "Channel360_Edit_Set": "edit_set_id",
    "Channel360_Patch_Plan_SHA256": "patch_plan_sha256",
}



def _format_signature(paragraph) -> tuple[str, dict[str, Any]]:
    """Short fingerprint of paragraph + run formatting, and a few readable properties of the first text run."""
    from lxml import etree
    pieces: list[bytes] = []
    for node in paragraph.xpath("./w:pPr", namespaces=NS):
        pieces.append(etree.tostring(node, method="c14n"))
    props: dict[str, Any] = {}
    first = True
    for run in paragraph.xpath(".//w:r[w:t]", namespaces=NS):
        rpr = run.xpath("./w:rPr", namespaces=NS)
        if rpr:
            pieces.append(etree.tostring(rpr[0], method="c14n"))
        if first:
            first = False
            def flag(tag: str) -> bool:
                return bool(run.xpath(f"./w:rPr/w:{tag}[not(@w:val) or not(@w:val=\"0\" or @w:val=\"false\" or @w:val=\"none\")]", namespaces=NS))
            props = {"bold": flag("b"), "italic": flag("i"), "underline": flag("u"),
                     "color": (run.xpath("./w:rPr/w:color/@w:val", namespaces=NS) or [None])[0],
                     "size": (run.xpath("./w:rPr/w:sz/@w:val", namespaces=NS) or [None])[0],
                     "font": (run.xpath("./w:rPr/w:rFonts/@w:ascii", namespaces=NS) or [None])[0],
                     "align": (paragraph.xpath("./w:pPr/w:jc/@w:val", namespaces=NS) or [None])[0]}
            if props["size"] and str(props["size"]).isdigit():
                props["size"] = int(props["size"]) / 2
    digest = hashlib.sha256(b"\x00".join(pieces)).hexdigest()[:12]
    return digest, {k: v for k, v in props.items() if v not in (None, False)}

class DocxDocumentError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DocxTarget:
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
    native_locator: dict[str, Any] | None = None
    part: str = "word/document.xml"


def _xml_parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                           remove_blank_text=False, huge_tree=False, recover=False)


def _parse(data: bytes) -> etree._Element:
    return etree.fromstring(data, parser=_xml_parser())


def _c14n(node: etree._Element) -> bytes:
    return etree.tostring(node, method="c14n", exclusive=False, with_comments=True)


def _fingerprint(node: etree._Element) -> str:
    clone = StdET.fromstring(etree.tostring(node, encoding="utf-8"))
    canonical = StdET.canonicalize(StdET.tostring(clone, encoding="unicode"), with_comments=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _parts(source: bytes) -> tuple[list[tuple[zipfile.ZipInfo, bytes]], dict[str, bytes]]:
    try:
        with zipfile.ZipFile(io.BytesIO(source)) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)):
                raise DocxDocumentError("duplicate_archive_member")
            if len(infos) > 5000 or sum(item.file_size for item in infos) > 250 * 1024 * 1024:
                raise DocxDocumentError("zip_expanded_size_limit")
            if any(posixpath.normpath(name).startswith("../") or name.startswith(("/", "\\")) or "\\" in name
                   for name in names):
                raise DocxDocumentError("unsafe_archive_member")
            bad = archive.testzip()
            if bad:
                raise DocxDocumentError("corrupt_archive_member")
            ordered = [(item, archive.read(item.filename)) for item in infos]
            return ordered, {item.filename: content for item, content in ordered}
    except DocxDocumentError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, KeyError) as exc:
        raise DocxDocumentError("invalid_or_unsafe_docx") from exc


def _parent(node: etree._Element, name: str) -> etree._Element | None:
    return node.getparent().xpath(f"ancestor-or-self::w:{name}[1]", namespaces=NS)[0] if node.getparent() and node.getparent().xpath(f"ancestor-or-self::w:{name}[1]", namespaces=NS) else None


def _target_location(part: str, paragraph_index: int, run_index: int, text_index: int) -> str:
    return f"{part}:p{paragraph_index}:r{run_index}:t{text_index}"


class DocxAdapter:
    version = "0.1.0"

    def inspect(self, source: bytes):
        from packages.core.models.contracts import CapabilityProfile

        _, parts = _parts(source)
        if "word/document.xml" not in parts or "[Content_Types].xml" not in parts:
            raise DocxDocumentError("not_docx")
        roots = {name: _parse(data) for name, data in parts.items()
                 if name.endswith((".xml", ".rels")) or name == "[Content_Types].xml"}
        reasons = self._document_reasons(parts, roots)
        targets = self.targets(source, roots=roots)
        features = ["ooxml_zip", "body", "tables"]
        features.extend("headers" for name in parts if re.fullmatch(r"word/header\d+\.xml", name))
        features.extend("footers" for name in parts if re.fullmatch(r"word/footer\d+\.xml", name))
        if any(name.endswith("vbaProject.bin") for name in parts):
            features.append("macros_present")
        if not any(target.editable for target in targets):
            reasons.append("no_supported_single_run_targets")
        level = "editable" if any(target.editable for target in targets) and not self._document_reasons(parts, roots) else "read_only"
        return CapabilityProfile(level, tuple(dict.fromkeys(reasons)), tuple(dict.fromkeys(features)), (), "docx", self.version)

    @staticmethod
    def _document_reasons(parts: dict[str, bytes], roots: dict[str, etree._Element]) -> list[str]:
        reasons: list[str] = []
        if any(name.lower().endswith("vbaproject.bin") for name in parts):
            reasons.append("macros_present")
        settings = roots.get("word/settings.xml")
        if settings is not None and settings.xpath(".//w:documentProtection", namespaces=NS):
            reasons.append("document_protected")
        for name, root in roots.items():
            if name.startswith("word/") and root.xpath(".//w:ins | .//w:del | .//w:moveFrom | .//w:moveTo", namespaces=NS):
                reasons.append("tracked_changes_present")
                break
        return reasons

    def targets(self, source: bytes, *, roots: dict[str, etree._Element] | None = None) -> list[DocxTarget]:
        ordered, parts = _parts(source)
        roots = roots or {name: _parse(data) for name, data in parts.items()
                          if name.endswith((".xml", ".rels")) or name == "[Content_Types].xml"}
        document_reasons = self._document_reasons(parts, roots)
        targets: list[DocxTarget] = []
        for part, root in roots.items():
            if not (part == "word/document.xml" or re.fullmatch(r"word/(?:header|footer)\d+\.xml", part)):
                continue
            for paragraph_index, paragraph in enumerate(root.xpath(".//w:p", namespaces=NS)):
                paragraph_runs = paragraph.xpath(".//w:r", namespaces=NS)
                paragraph_text = "".join(paragraph.xpath(".//w:t/text()", namespaces=NS))
                contains_field_code = bool(paragraph.xpath(".//w:fldChar | .//w:instrText | .//w:fldSimple", namespaces=NS))
                if len(paragraph_runs) > 1 and paragraph_text:
                    synthetic_id = f"docx:{part}:p{paragraph_index}:multi_run"
                    targets.append(DocxTarget(synthetic_id, "", part, str(paragraph_index),
                                              TypedValue("string", paragraph_text),
                                              hashlib.sha256(paragraph_text.encode()).hexdigest(),
                                              editable=False, reason="multi_run_span", label=paragraph_text[:120],
                                              native_locator={"part": part, "paragraph_index": paragraph_index,
                                                              "element": "w:p"}, part=part))
                run_index = 0
                for run in paragraph_runs:
                    text_nodes = run.xpath("./w:t", namespaces=NS)
                    for text_index, node in enumerate(text_nodes):
                        text = node.text or ""
                        if not text:
                            continue
                        reason = next(iter(document_reasons), None)
                        if reason is None and contains_field_code:
                            reason = "field_code"
                        if reason is None and node.xpath("ancestor::w:hyperlink", namespaces=NS):
                            reason = "hyperlink"
                        if reason is None and node.xpath("ancestor::w:sdt | ancestor::w:txbxContent", namespaces=NS):
                            reason = "content_control_or_textbox"
                        if reason is None and node.xpath("ancestor::w:ins | ancestor::w:del | ancestor::w:moveFrom | ancestor::w:moveTo", namespaces=NS):
                            reason = "tracked_change_region"
                        location = _target_location(part, paragraph_index, run_index, text_index)
                        identifier = f"docx:{location}"
                        targets.append(DocxTarget(identifier, "", part, str(paragraph_index), TypedValue("string", text),
                                                  _fingerprint(node), editable=reason is None, reason=reason,
                                                  label=text[:120], native_locator={"part": part,
                                                      "paragraph_index": paragraph_index, "run_index": run_index,
                                                      "text_index": text_index, "element": "w:t"}, part=part))
                    run_index += 1
        return targets

    def details(self, source: bytes, *, include_targets: bool) -> dict[str, Any]:
        _, parts = _parts(source)
        roots = {name: _parse(data) for name, data in parts.items()
                 if name.endswith((".xml", ".rels")) or name == "[Content_Types].xml"}
        blocks: list[dict[str, Any]] = []
        for part, root in roots.items():
            if not (part == "word/document.xml" or re.fullmatch(r"word/(?:header|footer)\d+\.xml", part)):
                continue
            for paragraph_index, paragraph in enumerate(root.xpath(".//w:p", namespaces=NS)):
                text = "".join(paragraph.xpath(".//w:t/text()", namespaces=NS))
                if text:
                    block = {"id": f"{part}:p{paragraph_index}", "part": part,
                             "paragraph_index": paragraph_index, "text": text,
                             "kind": "header" if "header" in part else "footer" if "footer" in part else
                                     "table_cell" if paragraph.xpath("ancestor::w:tc", namespaces=NS) else "paragraph"}
                    cells = paragraph.xpath("ancestor::w:tc", namespaces=NS)
                    if cells and part == "word/document.xml":
                        cell = cells[0]
                        row = cell.getparent()
                        table = row.getparent() if row is not None else None
                        if row is not None and table is not None:
                            block["table"] = {
                                "index": len(table.xpath("preceding::w:tbl", namespaces=NS)),
                                "row": len(row.xpath("preceding-sibling::w:tr", namespaces=NS)),
                                "col": len(cell.xpath("preceding-sibling::w:tc", namespaces=NS))}
                    block["fmt"], block["props"] = _format_signature(paragraph)
                    style_ids = paragraph.xpath("./w:pPr/w:pStyle/@w:val", namespaces=NS)
                    outline_levels = paragraph.xpath("./w:pPr/w:outlineLvl/@w:val", namespaces=NS)
                    style_match = re.fullmatch(r"Heading([1-9])", style_ids[0]) if style_ids else None
                    if style_match:
                        block["heading_level"] = int(style_match.group(1))
                    elif outline_levels and outline_levels[0].isdigit() and int(outline_levels[0]) < 9:
                        block["heading_level"] = int(outline_levels[0]) + 1
                    blocks.append(block)
        target_list = self.targets(source, roots=roots) if include_targets else []
        targets_by_block: dict[str, list[str]] = {}
        for target in target_list:
            locator = target.native_locator or {}
            key = f"{target.part}:p{locator.get('paragraph_index')}"
            targets_by_block.setdefault(key, []).append(target.id)
        for block in blocks:
            block["target_ids"] = targets_by_block.get(block["id"], [])
        media = [{"name": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                 for name, data in sorted(parts.items()) if re.fullmatch(r"(?:word/)?media/[^/]+", name)][:200]
        return {"paragraph_count": len(blocks), "blocks": blocks, "media": media,
                "document_properties": {"has_macros": any(name.lower().endswith("vbaproject.bin") for name in parts),
                                        "protected": "document_protected" in self._document_reasons(parts, roots),
                                        "tracked_changes": "tracked_changes_present" in self._document_reasons(parts, roots)},
                "targets": [{"id": target.id, "part": target.part, "text": target.value.value,
                             "editable": target.editable, "reason": target.reason,
                             "fingerprint": target.fingerprint, "native_locator": target.native_locator}
                            for target in target_list]}

    def unsupported_targets(self, source: bytes) -> list[DocxTarget]:
        return [target for target in self.targets(source) if not target.editable]

    def formula_dependents(self, source: bytes, target: DocxTarget):
        return (), False

    def type_compatible(self, old: TypedValue, new: TypedValue) -> bool:
        return old.type == new.type == "string"

    def validate_new_value(self, value: TypedValue) -> None:
        if value.type != "string" or not isinstance(value.value, str):
            raise DocxDocumentError("docx_text_requires_string")

    def permission_for(self, source: bytes, target: DocxTarget, new: TypedValue, provenance: dict[str, str]):
        _, parts = _parts(source)
        locator = target.id.removeprefix("docx:")
        permissions = [ChangePermission(target.part, locator, "replace_value", target.fingerprint,
                                        hashlib.sha256(str(new.value).encode()).hexdigest())]
        for part in ("docProps/custom.xml", "[Content_Types].xml", "_rels/.rels"):
            old = hashlib.sha256(parts[part]).hexdigest() if part in parts else None
            permissions.append(ChangePermission(part, None, "replace_part" if old else "add_part", old,
                                                "provenance:" + hashlib.sha256(
                                                    json.dumps(sorted(provenance.items()), separators=(",", ":")).encode()).hexdigest()))
        return permissions

    def preflight(self, source: bytes, target: DocxTarget, patch: Patch) -> etree._Element:
        if hashlib.sha256(source).hexdigest() != patch.source_version_id:
            raise DocxDocumentError("stale_source")
        fresh = next((item for item in self.targets(source) if item.id == patch.target_id), None)
        if fresh is None or not fresh.editable:
            raise DocxDocumentError(fresh.reason if fresh else "target_not_found")
        if fresh.fingerprint != patch.expected_fingerprint:
            raise DocxDocumentError("fingerprint_mismatch")
        if fresh.value.value != patch.expected_old_value.value:
            raise DocxDocumentError("expected_value_mismatch")
        self.validate_new_value(patch.new_value)
        return _resolve_target_node(source, fresh)

    def apply(self, source: bytes, plan: PatchPlan) -> bytes:
        if plan.no_change or not plan.patches:
            raise DocxDocumentError("no_change_has_no_candidate")
        ordered, parts = _parts(source)
        roots = {name: _parse(data) for name, data in parts.items()
                 if name.endswith((".xml", ".rels")) or name == "[Content_Types].xml"}
        for patch in plan.patches:
            target = next((item for item in self.targets(source, roots=roots) if item.id == patch.target_id), None)
            if target is None:
                raise DocxDocumentError("target_not_found")
            self.preflight(source, target, patch)
            root = roots[target.part]
            node = _node_at(root, target.native_locator or {})
            node.text = str(patch.new_value.value)
            text = node.text or ""
            if text[:1].isspace() or text[-1:].isspace():
                node.set(f"{{{XML}}}space", "preserve")
            else:
                node.attrib.pop(f"{{{XML}}}space", None)
        for part in roots:
            if part in parts and part in {target.part for target in self.targets(source)}:
                parts[part] = etree.tostring(roots[part], xml_declaration=True, encoding="UTF-8", standalone=True)
        _write_provenance(parts, roots, plan)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.comment = b""
            for info, _ in ordered:
                archive.writestr(info, parts[info.filename])
            for name in ("docProps/custom.xml", "[Content_Types].xml", "_rels/.rels"):
                if name not in {info.filename for info, _ in ordered}:
                    archive.writestr(name, parts[name], compress_type=zipfile.ZIP_DEFLATED)
        return output.getvalue()


def _node_at(root: etree._Element, locator: dict[str, Any]) -> etree._Element:
    paragraphs = root.xpath(".//w:p", namespaces=NS)
    paragraph = paragraphs[int(locator["paragraph_index"])]
    runs = paragraph.xpath(".//w:r", namespaces=NS)
    run = runs[int(locator["run_index"])]
    return run.xpath("./w:t", namespaces=NS)[int(locator["text_index"])]


def _resolve_target_node(source: bytes, target: DocxTarget) -> etree._Element:
    _, parts = _parts(source)
    return _node_at(_parse(parts[target.part]), target.native_locator or {})


def _write_provenance(parts: dict[str, bytes], roots: dict[str, etree._Element], plan: PatchPlan) -> None:
    custom_name = "docProps/custom.xml"
    custom = roots.get(custom_name)
    if custom is None:
        custom = etree.Element(f"{{{CUSTOM}}}Properties", nsmap={None: CUSTOM, "vt": VT})
    existing = {element.get("name"): element for element in custom.xpath("./cp:property", namespaces=NS)}
    props = plan.provenance_dict() | {"source_sha256": plan.source_sha256,
                                    "patch_plan_sha256": plan.plan_sha256}
    for name, source_key in PROVENANCE.items():
        prop = existing.get(name)
        if prop is None:
            prop = etree.SubElement(custom, f"{{{CUSTOM}}}property", fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}",
                                    pid=str(max([int(item.get("pid", "1")) for item in custom] + [1]) + 1), name=name)
        for child in list(prop):
            prop.remove(child)
        etree.SubElement(prop, f"{{{VT}}}lpwstr").text = props.get(source_key, "")
    parts[custom_name] = etree.tostring(custom, xml_declaration=True, encoding="UTF-8", standalone=True)
    roots[custom_name] = custom
    content_name = "[Content_Types].xml"
    content = roots[content_name]
    if not content.xpath("./ct:Override[@PartName='/docProps/custom.xml']", namespaces=NS):
        etree.SubElement(content, f"{{{CT}}}Override", PartName="/docProps/custom.xml", ContentType=CUSTOM_TYPE)
    parts[content_name] = etree.tostring(content, xml_declaration=True, encoding="UTF-8", standalone=True)
    relationships_name = "_rels/.rels"
    relationships = roots[relationships_name]
    if not relationships.xpath("./rel:Relationship[@Type=$kind]", namespaces=NS, kind=CUSTOM_REL):
        used = {relationship.get("Id") for relationship in relationships}
        index = 1
        while f"rId{index}" in used:
            index += 1
        etree.SubElement(relationships, f"{{{REL}}}Relationship", Id=f"rId{index}", Type=CUSTOM_REL,
                         Target="docProps/custom.xml")
    parts[relationships_name] = etree.tostring(relationships, xml_declaration=True, encoding="UTF-8", standalone=True)
