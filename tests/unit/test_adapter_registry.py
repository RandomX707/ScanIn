from __future__ import annotations

import io
import zipfile

from packages.core.adapters.registry import AdapterRegistry


def test_registry_selects_csv_by_content_and_reports_extension_mismatch():
    result = AdapterRegistry().inspect(b"claim_id,amount\nCLM-1,125\n", filename="policy.json")
    assert result.profile.adapter == "csv"
    assert result.profile.level == "editable"
    assert "extension_mismatch" in result.findings


def test_registry_selects_json_over_csv_when_json_signature_is_present():
    result = AdapterRegistry().inspect(b'{"claim":{"amount":125}}', filename="policy.csv")
    assert result.profile.adapter == "json"
    assert result.profile.level == "editable"
    assert "extension_mismatch" in result.findings


def test_registry_routes_docx_to_the_constrained_ooxml_adapter():
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as package:
        package.writestr("[Content_Types].xml", b"<Types />")
        package.writestr("word/document.xml", b"<document />")
    result = AdapterRegistry().inspect(content.getvalue(), filename="claim.xlsx")
    assert result.profile.level == "read_only"
    assert result.profile.adapter == "docx"
    assert result.profile.reasons == ("no_supported_single_run_targets",)
    assert "extension_mismatch" in result.findings


def test_registry_marks_unrecognized_binary_limited():
    result = AdapterRegistry().inspect(b"\x00\x01\x02opaque", filename="claim.bin")
    assert result.profile.level == "limited"
    assert "unknown_file_type" in result.profile.reasons
