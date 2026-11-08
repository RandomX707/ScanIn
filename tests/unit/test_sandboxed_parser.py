from __future__ import annotations

import io
import zipfile

import pytest

from packages.workers.parse_entry import inspect_bytes, ParserRejected


def _xlsx(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


def test_valid_xlsx_is_inspected_by_native_adapter():
    data = _xlsx({
        "[Content_Types].xml": b"<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types' />",
        "xl/workbook.xml": b"<workbook xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main' />",
        "xl/_rels/workbook.xml.rels": b"<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships' />",
    })
    result = inspect_bytes(data)
    assert result["media_type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert result["adapter"] == "xlsx"
    assert result["status"] == "inspected"


def test_content_first_registry_identifies_csv_with_mismatched_suffix():
    result = inspect_bytes(b"claim_id,amount\nCLM-1,125\n", filename="claim.json")
    assert result["adapter"] == "csv"
    assert result["media_type"] == "text/csv"
    assert result["findings"] == ["extension_mismatch"]
    assert result["capability"]["level"] == "editable"


def test_content_first_registry_identifies_json_with_mismatched_suffix():
    result = inspect_bytes(b'{"claim":{"amount":125}}', filename="claim.csv")
    assert result["adapter"] == "json"
    assert result["findings"] == ["extension_mismatch"]


def test_structural_summary_for_csv_uses_real_rows_and_recognized_headers():
    result = inspect_bytes(b"claim_number,policyholder,amount\nC-17,Example Person,125\nC-18,Another Person,80\n",
                           filename="claims.csv")
    summary = result["structural_summary"]
    assert summary["generation_method"] == "structural"
    assert summary["format"] == "csv"
    assert summary["apparent_document_type"] == "insurance claim document"
    assert summary["statistics"]["record_count"] == 2
    assert {item["name"] for item in summary["observed_fields"]} >= {"claim_number", "policyholder", "amount"}
    assert "2 data records" in summary["description"]
    assert "Example Person" not in summary["description"]


def test_structural_summary_for_json_describes_shape_without_inventing_a_type():
    result = inspect_bytes(b'{"envelope":{"items":[{"reference":"X-1"}]},"enabled":true}', filename="payload.json")
    summary = result["structural_summary"]
    assert summary["statistics"]["top_level_structure"] == "object"
    assert summary["apparent_document_type"] is None
    assert "envelope contains items" in summary["description"]
    assert "X-1" not in summary["description"]


def test_structural_summary_is_deterministic_and_unknown_type_is_honest():
    source = b"plain text without a supported document structure"
    first = inspect_bytes(source, filename="notes.txt")["structural_summary"]
    second = inspect_bytes(source, filename="notes.txt")["structural_summary"]
    assert first == second
    assert first["format"] == "text"
    assert first["apparent_document_type"] is None
    assert "unclear" in first["description"].lower()
    unsupported = inspect_bytes(b"\x89PNG\r\n\x1a\n", filename="image.png")["structural_summary"]
    assert unsupported["apparent_document_type"] is None
    assert "could not be read reliably" in unsupported["description"]


def test_structural_summary_xlsx_uses_sheet_dimensions_and_headers():
    from openpyxl import Workbook
    workbook = Workbook(); sheet = workbook.active; sheet.title = "Claims"
    sheet.append(["claim_number", "policyholder"]); sheet.append(["CLM-9", "Example"])
    sheet["C1"] = "=A2"
    output = io.BytesIO(); workbook.save(output)
    summary = inspect_bytes(output.getvalue(), filename="claims.xlsx")["structural_summary"]
    assert summary["format"] == "xlsx"
    assert summary["statistics"]["worksheets"][0]["rows"] == 2
    assert summary["statistics"]["worksheets"][0]["columns"] == 3
    assert summary["apparent_document_type"] == "insurance claim document"
    assert "=A2" not in summary["description"]


def test_restricted_entrypoint_returns_bounded_typed_targets_on_request():
    result = inspect_bytes(b"claim_id,amount\nCLM-1,125\n", filename="claim.csv", include_targets=True)
    assert result["targets"] == [
        {"id": "csv:1:1", "sheet": "", "cell": "1,1", "label": None,
         "value": {"type": "string", "value": "claim_id", "native": "claim_id", "display": None},
         "fingerprint": result["targets"][0]["fingerprint"], "editable": True, "reason": None, "dependents": []},
        {"id": "csv:1:2", "sheet": "", "cell": "1,2", "label": None,
         "value": {"type": "string", "value": "amount", "native": "amount", "display": None},
         "fingerprint": result["targets"][1]["fingerprint"], "editable": True, "reason": None, "dependents": []},
        {"id": "csv:2:1", "sheet": "", "cell": "2,1", "label": "claim_id",
         "value": {"type": "string", "value": "CLM-1", "native": "CLM-1", "display": None},
         "fingerprint": result["targets"][2]["fingerprint"], "editable": True, "reason": None, "dependents": []},
        {"id": "csv:2:2", "sheet": "", "cell": "2,2", "label": "amount",
         "value": {"type": "integer", "value": 125, "native": "125", "display": None},
         "fingerprint": result["targets"][3]["fingerprint"], "editable": True, "reason": None, "dependents": []},
    ]
    assert result["targets_truncated"] is False


def test_zip_expansion_bomb_is_rejected_before_adapter_load():
    data = _xlsx({
        "[Content_Types].xml": b"Types",
        "xl/workbook.xml": b"W" * 2_000_000,
        "xl/_rels/workbook.xml.rels": b"Relationships",
    })
    with pytest.raises(ParserRejected, match="zip_expansion_limit"):
        inspect_bytes(data, max_expansion_ratio=20)


def test_path_traversal_member_is_rejected():
    data = _xlsx({"../outside.txt": b"no extraction should happen"})
    with pytest.raises(ParserRejected, match="unsafe_archive_member"):
        inspect_bytes(data)


def test_malformed_docx_zip_is_rejected():
    data = _xlsx({"word/document.xml": b"<document />"})
    with pytest.raises(ParserRejected, match="not_docx"):
        inspect_bytes(data)


def test_nested_archives_are_rejected_without_extraction(tmp_path):
    nested = _xlsx({"deep.txt": b"data"})
    outer = _xlsx({"nested.zip": nested})
    with pytest.raises(ParserRejected, match="nested_archive_unsupported"):
        inspect_bytes(outer)


def test_duplicate_archive_members_are_rejected():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("same.xml", b"first")
        archive.writestr("same.xml", b"second")
    with pytest.raises(ParserRejected, match="duplicate_archive_member"):
        inspect_bytes(output.getvalue())
