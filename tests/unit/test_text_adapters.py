from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from packages.core.models.contracts import Patch, TypedValue
from packages.core.planning.planner import PlanError, build_patch_plan


def _plan(adapter, source: bytes, target_id: str, value: TypedValue):
    target = next(item for item in adapter.targets(source) + adapter.unsupported_targets(source)
                  if item.id == target_id)
    return build_patch_plan(
        adapter, source,
        (Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint, value),),
        provenance={"edit_set_id": "text-proof", "actor_id": "reviewer"},
    )


def test_csv_bom_windows1252_crlf_edit_changes_only_the_selected_cell():
    from packages.core.adapters.csv_adapter.adapter import CsvAdapter
    from packages.core.verification.text_adapters import CsvIndependentVerifier

    source = b'\xef\xbb\xbfclaim_id,amount,note\r\nCLM-1,125.50,"caf\xc3\xa9, front"\r\nCLM-2,-500,"line1\r\nline2"\r\n'
    adapter = CsvAdapter()
    profile = adapter.inspect(source)
    assert profile.level == "editable"
    assert {"utf8_bom", "crlf", "comma_delimited"} <= set(profile.features)
    target = next(item for item in adapter.targets(source) if item.id == "csv:2:3")
    assert target.value == TypedValue("string", "café, front", native='"café, front"')
    assert target.label == "note"
    plan = _plan(adapter, source, target.id, TypedValue("string", "café updated, rear"))
    output = adapter.apply(source, plan)
    assert output is not None and output != source
    assert source.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in output and b"\n" not in output.replace(b"\r\n", b"")
    assert b"CLM-2,-500,\"line1\r\nline2\"" in output
    report = CsvIndependentVerifier().verify(source, output, plan)
    assert report.outcome == "VERIFIED"
    assert source.startswith(b"\xef\xbb\xbfclaim_id")


def test_csv_rejects_formula_injection_without_altering_proposed_value():
    from packages.core.adapters.csv_adapter.adapter import CsvAdapter

    source = b"name,value\nclaim,100\n"
    target = next(item for item in CsvAdapter().targets(source) if item.id == "csv:2:2")
    patch = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint,
                  TypedValue("string", "=1+1"))
    with pytest.raises(PlanError, match="unsafe_csv_value"):
        build_patch_plan(CsvAdapter(), source, (patch,), provenance={"edit_set_id": "unsafe"})


@pytest.mark.parametrize("proposed", ["=1+1", "+SUM(A1:A2)", "@command", "\tvalue", "\rvalue"])
def test_csv_injection_prefixes_are_rejected_exactly(proposed: str):
    from packages.core.adapters.csv_adapter.adapter import CsvAdapter

    source = b"name,value\nclaim,ordinary\n"
    target = next(item for item in CsvAdapter().targets(source) if item.id == "csv:2:2")
    patch = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint,
                  TypedValue("string", proposed))
    with pytest.raises(PlanError) as error:
        build_patch_plan(CsvAdapter(), source, (patch,), provenance={"edit_set_id": "unsafe"})
    assert error.value.code == "unsafe_csv_value"


def test_csv_windows1252_is_detected_and_preserved():
    from packages.core.adapters.csv_adapter.adapter import CsvAdapter
    from packages.core.verification.text_adapters import CsvIndependentVerifier

    source = b"claim,note\r\nCLM-1,caf\xe9\r\n"
    adapter = CsvAdapter()
    profile = adapter.inspect(source)
    assert profile.level == "editable"
    assert "cp1252" in profile.features
    target = next(item for item in adapter.targets(source) if item.id == "csv:2:2")
    plan = _plan(adapter, source, target.id, TypedValue("string", "résumé"))
    output = adapter.apply(source, plan)
    assert output == b"claim,note\r\nCLM-1,r\xe9sum\xe9\r\n"
    assert CsvIndependentVerifier().verify(source, output, plan).outcome == "VERIFIED"


def test_csv_windows1252_rejects_unrepresentable_text_without_substitution():
    from packages.core.adapters.csv_adapter.adapter import CsvAdapter

    source = b"claim,note\nCLM-1,caf\xe9\n"
    adapter = CsvAdapter()
    target = next(item for item in adapter.targets(source) if item.id == "csv:2:2")
    with pytest.raises(PlanError) as error:
        _plan(adapter, source, target.id, TypedValue("string", "emoji 😀"))
    assert error.value.code == "value_not_representable_in_encoding"


def test_csv_negative_numeric_values_remain_numeric_and_are_allowed():
    from packages.core.adapters.csv_adapter.adapter import CsvAdapter
    from packages.core.verification.text_adapters import CsvIndependentVerifier

    source = b"claim,amount\nCLM-1,-500\n"
    adapter = CsvAdapter()
    target = next(item for item in adapter.targets(source) if item.id == "csv:2:2")
    assert target.value == TypedValue("integer", -500, native="-500")
    plan = _plan(adapter, source, target.id, TypedValue("integer", -750))
    output = adapter.apply(source, plan)
    assert output == b"claim,amount\nCLM-1,-750\n"
    assert CsvIndependentVerifier().verify(source, output, plan).outcome == "VERIFIED"


def test_csv_decimal_serialization_preserves_decimal_type_when_fraction_is_zero():
    from packages.core.adapters.csv_adapter.adapter import CsvAdapter
    from packages.core.verification.text_adapters import CsvIndependentVerifier

    source = b"claim,amount\nCLM-1,10\n"
    adapter = CsvAdapter()
    target = next(item for item in adapter.targets(source) if item.id == "csv:2:2")
    plan = _plan(adapter, source, target.id, TypedValue("decimal", Decimal("1000")))
    output = adapter.apply(source, plan)
    assert output == b"claim,amount\nCLM-1,1000.0\n"
    assert CsvIndependentVerifier().verify(source, output, plan).outcome == "VERIFIED"


def test_csv_mixed_line_endings_are_read_only():
    from packages.core.adapters.csv_adapter.adapter import CsvAdapter

    source = b"a,b\r\n1,2\n"
    profile = CsvAdapter().inspect(source)
    assert profile.level == "read_only"
    assert "mixed_line_endings" in profile.reasons


def test_csv_newlines_inside_quoted_cells_do_not_change_record_line_ending():
    from packages.core.adapters.csv_adapter.adapter import CsvAdapter
    from packages.core.verification.text_adapters import CsvIndependentVerifier

    source = b'claim,note\r\nCLM-1,"first line\nsecond line"\r\n'
    adapter = CsvAdapter()
    profile = adapter.inspect(source)
    assert profile.level == "editable"
    assert "crlf" in profile.features
    target = next(item for item in adapter.targets(source) if item.id == "csv:2:2")
    plan = _plan(adapter, source, target.id, TypedValue("string", "one line"))
    output = adapter.apply(source, plan)
    assert output == b"claim,note\r\nCLM-1,one line\r\n"
    assert CsvIndependentVerifier().verify(source, output, plan).outcome == "VERIFIED"


def test_json_nested_number_edit_preserves_order_and_every_other_byte():
    from packages.core.adapters.json_adapter.adapter import JsonAdapter
    from packages.core.verification.text_adapters import JsonIndependentVerifier

    source = b'{\n  "claim": {"id":"CLM-1","amount":125.50,"note":"Loss: \\u00e9"},\n  "ok": true\n}\n'
    adapter = JsonAdapter()
    profile = adapter.inspect(source)
    assert profile.level == "editable"
    target = next(item for item in adapter.targets(source) if item.id == "json:/claim/amount")
    assert target.value == TypedValue("decimal", Decimal("125.50"), native="125.50")
    plan = _plan(adapter, source, target.id, TypedValue("decimal", Decimal("130.75")))
    output = adapter.apply(source, plan)
    assert output is not None
    assert output == source.replace(b"125.50", b"130.75")
    report = JsonIndependentVerifier().verify(source, output, plan)
    assert report.outcome == "VERIFIED"


def test_json_explicit_patch_can_change_a_scalar_type():
    from packages.core.adapters.json_adapter.adapter import JsonAdapter
    from packages.core.verification.text_adapters import JsonIndependentVerifier

    source = b'{"amount":125,"unchanged":true}'
    adapter = JsonAdapter()
    target = next(item for item in adapter.targets(source) if item.id == "json:/amount")
    plan = _plan(adapter, source, target.id, TypedValue("string", "125 dollars"))
    output = adapter.apply(source, plan)
    assert output == b'{"amount":"125 dollars","unchanged":true}'
    assert JsonIndependentVerifier().verify(source, output, plan).outcome == "VERIFIED"


def test_json_uses_file_unicode_escape_style_for_replacement_strings():
    from packages.core.adapters.json_adapter.adapter import JsonAdapter
    from packages.core.verification.text_adapters import JsonIndependentVerifier

    source = b'{"note":"old","example":"\\u00e9"}'
    adapter = JsonAdapter()
    target = next(item for item in adapter.targets(source) if item.id == "json:/note")
    plan = _plan(adapter, source, target.id, TypedValue("string", "café"))
    output = adapter.apply(source, plan)
    assert output == b'{"note":"caf\\u00e9","example":"\\u00e9"}'
    assert JsonIndependentVerifier().verify(source, output, plan).outcome == "VERIFIED"


def test_json_duplicate_member_object_targets_are_explicitly_unsupported():
    from packages.core.adapters.json_adapter.adapter import JsonAdapter

    source = b'{"x":1,"x":2,"safe":3}'
    adapter = JsonAdapter()
    profile = adapter.inspect(source)
    assert profile.level == "editable"
    targets = adapter.unsupported_targets(source)
    assert {item.id for item in targets} == {"json:/x", "json:/safe"}
    assert all(item.reason == "duplicate_keys" for item in targets)


def test_json_non_utf8_is_read_only_not_rewritten():
    from packages.core.adapters.json_adapter.adapter import JsonAdapter

    source = b'{"name":"caf\xe9"}'
    profile = JsonAdapter().inspect(source)
    assert profile.level == "read_only"
    assert "non_utf8_json" in profile.reasons


def test_json_pointer_escapes_object_keys_and_array_indexes():
    from packages.core.adapters.json_adapter.adapter import JsonAdapter

    source = b'{"a/b":{"~key":["first","second"]}}'
    targets = JsonAdapter().targets(source)
    assert {item.id for item in targets} == {"json:/a~1b/~0key/0", "json:/a~1b/~0key/1"}


def test_json_candidate_tampering_is_blocked():
    from packages.core.adapters.json_adapter.adapter import JsonAdapter
    from packages.core.verification.text_adapters import JsonIndependentVerifier

    source = b'{"x":1,"safe":2}'
    adapter = JsonAdapter()
    target = next(item for item in adapter.targets(source) if item.id == "json:/x")
    plan = _plan(adapter, source, target.id, TypedValue("integer", 3))
    output = adapter.apply(source, plan)
    assert output is not None
    tampered = output.replace(b'"safe":2', b'"safe":99')
    assert JsonIndependentVerifier().verify(source, tampered, plan).outcome == "BLOCKED"


def test_csv_native_release_returns_verified_new_copy_without_touching_original():
    from packages.core.adapters.csv_adapter.adapter import CsvAdapter
    from packages.core.verification.text_adapters import CsvIndependentVerifier
    from packages.workflow.release import NativeReleaseWorkflow

    source = b"claim,amount\nCLM-1,125.50\n"
    original = bytes(source)
    adapter = CsvAdapter()
    target = next(item for item in adapter.targets(source) if item.id == "csv:2:2")
    patch = Patch(target.version_id, target.id, target.value, target.fingerprint, TypedValue("decimal", Decimal("130.75")))
    frozen = NativeReleaseWorkflow(adapter, CsvIndependentVerifier()).freeze(
        source=source, document_id="document-1", base_version_id="version-1", actor_id="reviewer-1",
        role="OWNER", authorized_category="insurer-authored", reason="Fix approved amount", patches=(patch,),
    )
    result = NativeReleaseWorkflow(adapter, CsvIndependentVerifier()).release(source=source, edit_set=frozen)
    assert source == original
    assert result.status == "VERIFIED"
    assert result.derived_bytes == b"claim,amount\nCLM-1,130.75\n"


def test_csv_independent_verifier_blocks_tampered_candidate_after_frozen_plan():
    from packages.core.adapters.csv_adapter.adapter import CsvAdapter
    from packages.core.verification.text_adapters import CsvIndependentVerifier

    source = b"claim,amount\nCLM-1,125.50\n"
    adapter = CsvAdapter()
    target = next(item for item in adapter.targets(source) if item.id == "csv:2:1")
    plan = _plan(adapter, source, target.id, TypedValue("string", "CLM-2"))
    authorized = adapter.apply(source, plan)
    assert authorized == b"claim,amount\nCLM-2,125.50\n"
    tampered = authorized.replace(b"125.50", b"999.99")

    report = CsvIndependentVerifier().verify(source, tampered, plan)

    assert report.outcome == "BLOCKED"
    assert report.check("preservation").status == "FAIL"


def test_json_native_release_returns_verified_new_copy():
    from packages.core.adapters.json_adapter.adapter import JsonAdapter
    from packages.core.verification.text_adapters import JsonIndependentVerifier
    from packages.workflow.release import NativeReleaseWorkflow

    source = b'{"claim":{"amount":125},"ok":true}'
    adapter = JsonAdapter()
    target = next(item for item in adapter.targets(source) if item.id == "json:/claim/amount")
    patch = Patch(target.version_id, target.id, target.value, target.fingerprint, TypedValue("integer", 130))
    workflow = NativeReleaseWorkflow(adapter, JsonIndependentVerifier())
    frozen = workflow.freeze(source=source, document_id="document-1", base_version_id="version-1",
                             actor_id="processor-1", role="OWNER", authorized_category="insurer-authored",
                             reason="Correct claim value", patches=(patch,))
    result = workflow.release(source=source, edit_set=frozen)
    assert result.status == "VERIFIED"
    assert result.derived_bytes == b'{"claim":{"amount":130},"ok":true}'
