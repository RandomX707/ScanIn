from __future__ import annotations

import hashlib

import pytest

from packages.core.adapters.csv_adapter.adapter import CsvAdapter, CsvError
from packages.core.models.contracts import Patch, TypedValue
from packages.core.planning.planner import build_patch_plan
from packages.core.verification.text_adapters import CsvIndependentVerifier, _csv_document
from packages.workers.parse_entry import inspect_bytes


def _boundary_utf8_csv() -> bytes:
    """Valid UTF-8 whose two-byte 'é' straddles the 8,192-byte dialect sample boundary."""
    head = b"claim,insured\n"
    filler = b"CLM-1," + b"a" * (8192 - len(head) - 6 - 1)
    source = head + filler + "é".encode() + b"\nCLM-2,b\n"
    assert source[8191:8193] == "é".encode()
    return source


def test_cp1252_guess_is_reported_but_policy_keeps_it_editable():
    source = b"claim,insured,amount\nCLM-10,Neha Rao,1000\nCLM-11,\xff\xfe,2000\n"
    profile = CsvAdapter().inspect(source)
    assert profile.level == "editable"
    assert profile.reasons == ("encoding_guessed_cp1252",)
    assert "cp1252" in profile.features
    result = inspect_bytes(source, filename="claims.csv", include_targets=True)
    assert result["capability"] == {"level": "editable", "reasons": ["encoding_guessed_cp1252"]}
    assert next(item for item in result["targets"] if item["id"] == "csv:3:2")["value"]["value"] == "ÿþ"


@pytest.mark.parametrize("source", [b"claim,note\nCLM-1,caf\xc3\xa9\n", b"\xef\xbb\xbfclaim,note\nCLM-1,x\n"])
def test_utf8_documents_carry_no_encoding_notice(source):
    assert CsvAdapter().inspect(source).reasons == ()


@pytest.mark.parametrize("undefined", [b"\x81", b"\x8d", b"\x8f", b"\x90", b"\x9d"])
def test_bytes_undefined_in_cp1252_are_never_replaced(undefined):
    source = b"claim,note\nCLM-1,x" + undefined + b"y\n"
    profile = CsvAdapter().inspect(source)
    assert profile.level == "read_only" and profile.reasons == ("unsupported_encoding",)
    with pytest.raises(CsvError, match="unsupported_encoding"):
        CsvAdapter().targets(source)


def test_utf8_sample_boundary_does_not_break_inspection_or_verification():
    source = _boundary_utf8_csv()
    adapter = CsvAdapter()
    profile = adapter.inspect(source)
    assert profile.level == "editable" and profile.reasons == () and "utf-8" in profile.features
    assert _csv_document(source)[(2, 2)][3].endswith("é")
    target = next(item for item in adapter.targets(source) if item.id == "csv:3:2")
    plan = build_patch_plan(adapter, source, (Patch(hashlib.sha256(source).hexdigest(), target.id, target.value,
                                                    target.fingerprint, TypedValue("string", "Rāhul")),),
                            provenance={"edit_set_id": "boundary", "actor_id": "owner"})
    output = adapter.apply(source, plan)
    assert output == source[:-2] + "Rāhul".encode() + b"\n"
    assert CsvIndependentVerifier().verify(source, output, plan).outcome == "VERIFIED"


def test_independent_verifier_blocks_when_neither_encoding_decodes():
    source = b"claim,note\nCLM-1,caf\xe9\n"
    adapter = CsvAdapter()
    target = next(item for item in adapter.targets(source) if item.id == "csv:2:2")
    plan = build_patch_plan(adapter, source, (Patch(hashlib.sha256(source).hexdigest(), target.id, target.value,
                                                    target.fingerprint, TypedValue("string", "cafe")),),
                            provenance={"edit_set_id": "tamper", "actor_id": "owner"})
    tampered = b"claim,note\nCLM-1,caf\x81\n"
    report = CsvIndependentVerifier().verify(source, tampered, plan)
    assert report.outcome == "BLOCKED"
