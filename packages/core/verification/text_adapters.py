from __future__ import annotations

import codecs

import csv
import hashlib
import io
import json
import re
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from packages.core.models.contracts import CheckResult, PatchPlan, TypedValue, VerifyReport


class CsvIndependentVerifier:
    """CSV read-back and byte-boundary verifier independent of CsvAdapter."""

    def verify(self, source: bytes, candidate: bytes, plan: PatchPlan) -> VerifyReport:
        source_hash, output_hash = hashlib.sha256(source).hexdigest(), hashlib.sha256(candidate).hexdigest()
        if _plan_hash(plan) != plan.plan_sha256 or source_hash != plan.source_sha256:
            return _blocked(source_hash, output_hash, "policy", "frozen_plan_or_source_mismatch")
        try:
            source_doc = _csv_document(source)
            output_doc = _csv_document(candidate)
        except Exception as error:
            return _blocked(source_hash, output_hash, "integrity", f"csv_parse_failed:{type(error).__name__}")
        results: list[CheckResult] = [CheckResult("integrity", "PASS", True, {"source_and_candidate_parse": True})]
        expected_ids = {patch.target_id for patch in plan.patches}
        permitted = [item for item in plan.permitted_changes if item.operation == "replace_value" and item.part == "body"]
        policy_errors: list[str] = []
        if expected_ids != {item.locator for item in permitted}:
            policy_errors.append("permitted_target_set_mismatch")
        if len(permitted) != len(plan.permitted_changes):
            policy_errors.append("permission_for_unknown_operation_or_part")
        if not plan.patches or plan.no_change:
            policy_errors.append("no_change_has_no_candidate")
        if policy_errors:
            results.append(CheckResult("policy", "FAIL", True, {"errors": policy_errors}))
        else:
            results.append(CheckResult("policy", "PASS", True, {"all_targets_authorized": True}))
        spans_old: list[tuple[int, int]] = []
        spans_new: list[tuple[int, int]] = []
        intent_errors: list[str] = []
        permission_by_target = {item.locator: item for item in permitted}
        for patch in plan.patches:
            try:
                row, column = _csv_target(patch.target_id)
                old = source_doc[(row, column)]
                new = output_doc[(row, column)]
            except (KeyError, ValueError):
                intent_errors.append(f"{patch.target_id}:target_missing")
                continue
            spans_old.append((old[0], old[1]))
            spans_new.append((new[0], new[1]))
            if hashlib.sha256(old[2]).hexdigest() != patch.expected_fingerprint:
                intent_errors.append(f"{patch.target_id}:source_fingerprint_mismatch")
            if not _matches_typed(_csv_typed(old[3], old[2], old[4]), patch.expected_old_value):
                intent_errors.append(f"{patch.target_id}:old_value_mismatch")
            actual = _csv_typed(new[3], new[2], new[4])
            if not _matches_typed(actual, patch.new_value):
                intent_errors.append(f"{patch.target_id}:new_value_or_type_mismatch")
            if patch.new_value.type == "string" and str(patch.new_value.value).startswith(("=", "+", "-", "@", "\t", "\r")):
                intent_errors.append(f"{patch.target_id}:unsafe_csv_value")
            permission = permission_by_target.get(patch.target_id)
            if permission is None or hashlib.sha256(new[2]).hexdigest() != permission.expected_new_fingerprint:
                intent_errors.append(f"{patch.target_id}:permission_fingerprint_mismatch")
        results.append(CheckResult("intent", "FAIL" if intent_errors else "PASS", True, {"errors": intent_errors}))
        preserved = _without_spans(source, spans_old) == _without_spans(candidate, spans_new)
        results.append(CheckResult("preservation", "PASS" if preserved else "FAIL", True,
                                   {"all_bytes_outside_authorized_cells_identical": preserved}))
        provenance_ok = bool(plan.provenance_dict().get("edit_set_id") and plan.provenance_dict().get("actor_id"))
        results.append(CheckResult("provenance", "PASS" if provenance_ok else "FAIL", True,
                                   {"frozen_plan_has_edit_set_and_actor": provenance_ok}))
        results.append(CheckResult("dependents", "PASS", False, {"formula_dependencies": []}))
        return _report(source_hash, output_hash, results)


class JsonIndependentVerifier:
    """JSON verification with a separate standard-library structural scanner."""

    def verify(self, source: bytes, candidate: bytes, plan: PatchPlan) -> VerifyReport:
        source_hash, output_hash = hashlib.sha256(source).hexdigest(), hashlib.sha256(candidate).hexdigest()
        if _plan_hash(plan) != plan.plan_sha256 or source_hash != plan.source_sha256:
            return _blocked(source_hash, output_hash, "policy", "frozen_plan_or_source_mismatch")
        try:
            old_nodes = _json_nodes(source)
            new_nodes = _json_nodes(candidate)
        except Exception as error:
            return _blocked(source_hash, output_hash, "integrity", f"json_parse_failed:{type(error).__name__}")
        results: list[CheckResult] = [CheckResult("integrity", "PASS", True, {"source_and_candidate_parse": True})]
        expected_ids = {patch.target_id for patch in plan.patches}
        permitted = [item for item in plan.permitted_changes if item.operation == "replace_value" and item.part == "body"]
        policy_errors: list[str] = []
        if expected_ids != {item.locator for item in permitted}:
            policy_errors.append("permitted_target_set_mismatch")
        if len(permitted) != len(plan.permitted_changes):
            policy_errors.append("permission_for_unknown_operation_or_part")
        if not plan.patches or plan.no_change:
            policy_errors.append("no_change_has_no_candidate")
        results.append(CheckResult("policy", "FAIL" if policy_errors else "PASS", True,
                                   {"errors": policy_errors, "all_targets_authorized": not policy_errors}))
        old_by_pointer = {node.pointer: node for node in old_nodes}
        new_by_pointer = {node.pointer: node for node in new_nodes}
        old_spans: list[tuple[int, int]] = []
        new_spans: list[tuple[int, int]] = []
        errors: list[str] = []
        permissions = {item.locator: item for item in permitted}
        for patch in plan.patches:
            pointer = patch.target_id[5:] if patch.target_id.startswith("json:") else None
            if pointer is None or pointer not in old_by_pointer or pointer not in new_by_pointer:
                errors.append(f"{patch.target_id}:target_missing")
                continue
            old, new = old_by_pointer[pointer], new_by_pointer[pointer]
            old_spans.append((old.start, old.end))
            new_spans.append((new.start, new.end))
            if old.duplicate or new.duplicate:
                errors.append(f"{patch.target_id}:duplicate_keys")
            if hashlib.sha256(old.raw).hexdigest() != patch.expected_fingerprint:
                errors.append(f"{patch.target_id}:source_fingerprint_mismatch")
            if not _matches_typed(old.value, patch.expected_old_value):
                errors.append(f"{patch.target_id}:old_value_mismatch")
            if not _matches_typed(new.value, patch.new_value):
                errors.append(f"{patch.target_id}:new_value_or_type_mismatch")
            permission = permissions.get(patch.target_id)
            if permission is None or hashlib.sha256(new.raw).hexdigest() != permission.expected_new_fingerprint:
                errors.append(f"{patch.target_id}:permission_fingerprint_mismatch")
        results.append(CheckResult("intent", "FAIL" if errors else "PASS", True, {"errors": errors}))
        preserved = _without_spans(source, old_spans) == _without_spans(candidate, new_spans)
        results.append(CheckResult("preservation", "PASS" if preserved else "FAIL", True,
                                   {"all_bytes_outside_authorized_tokens_identical": preserved}))
        provenance_ok = bool(plan.provenance_dict().get("edit_set_id") and plan.provenance_dict().get("actor_id"))
        results.append(CheckResult("provenance", "PASS" if provenance_ok else "FAIL", True,
                                   {"frozen_plan_has_edit_set_and_actor": provenance_ok}))
        results.append(CheckResult("dependents", "PASS", False, {"formula_dependencies": []}))
        return _report(source_hash, output_hash, results)


def _csv_document(data: bytes) -> dict[tuple[int, int], tuple[int, int, bytes, str, bool]]:
    bom = data.startswith(b"\xef\xbb\xbf")
    start = 3 if bom else 0
    payload = data[start:]
    # Independent encoding decision: the whole payload must decode strictly as UTF-8 (always when a
    # BOM is present) or, failing that, as Windows-1252. Nothing is ever decoded with replacement.
    encoding = "utf-8"
    try:
        payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        if bom:
            raise
        encoding = "cp1252"
        payload.decode(encoding, errors="strict")
    sample = codecs.getincrementaldecoder(encoding)(errors="strict").decode(payload[:8192], final=False)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    delimiter, quotechar = ord(dialect.delimiter), ord(dialect.quotechar or '"')
    result: dict[tuple[int, int], tuple[int, int, bytes, str, bool]] = {}
    row = column = 1
    pos = start
    while pos < len(data):
        if data[pos] in (10, 13):
            pos = pos + 2 if data[pos:pos + 2] == b"\r\n" else pos + 1
            row, column = row + 1, 1
            continue
        begin = pos
        quoted = data[pos] == quotechar
        if quoted:
            pos += 1
            while pos < len(data):
                if data[pos] == quotechar:
                    if pos + 1 < len(data) and data[pos + 1] == quotechar:
                        pos += 2
                    else:
                        pos += 1
                        break
                else:
                    pos += 1
        else:
            while pos < len(data) and data[pos] not in (delimiter, 10, 13):
                pos += 1
        raw = data[begin:pos]
        if not raw:
            value = ""
        else:
            parsed = list(csv.reader(io.StringIO(raw.decode(encoding), newline=""), delimiter=chr(delimiter),
                                     quotechar=chr(quotechar), doublequote=True, strict=True))
            if len(parsed) != 1 or len(parsed[0]) != 1:
                raise ValueError("csv_token_invalid")
            value = parsed[0][0]
        result[(row, column)] = (begin, pos, raw, value, quoted)
        if pos < len(data) and data[pos] == delimiter:
            pos += 1
            column += 1
        elif pos < len(data) and data[pos] in (10, 13):
            pos = pos + 2 if data[pos:pos + 2] == b"\r\n" else pos + 1
            row, column = row + 1, 1
    return result


def _json_nodes(data: bytes) -> list[Any]:
    import json as std_json
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Node:
        pointer: str
        start: int
        end: int
        raw: bytes
        value: TypedValue
        duplicate: bool = False

    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ValueError("non_utf8_json") from None
    index = 0
    nodes: list[Node] = []

    def whitespace() -> None:
        nonlocal index
        while index < len(data) and data[index] in b" \t\r\n":
            index += 1

    def string_end(start: int) -> int:
        cursor = start + 1
        while cursor < len(data):
            if data[cursor] == 92:
                cursor += 2
            elif data[cursor] == 34:
                return cursor + 1
            elif data[cursor] < 32:
                raise ValueError("control_in_string")
            else:
                cursor += 1
        raise ValueError("unterminated_string")

    def parse(pointer: str) -> Any:
        nonlocal index
        whitespace()
        if index >= len(data):
            raise ValueError("unexpected_eof")
        token = data[index]
        if token == 123:
            index += 1
            whitespace()
            values: dict[str, Any] = {}
            seen: set[str] = set()
            duplicate = False
            first_node = len(nodes)
            if index < len(data) and data[index] == 125:
                index += 1
                return values
            while True:
                whitespace()
                if data[index] != 34:
                    raise ValueError("object_key_expected")
                end_key = string_end(index)
                key = std_json.loads(data[index:end_key].decode("utf-8"))
                duplicate |= key in seen
                seen.add(key)
                index = end_key
                whitespace()
                if data[index] != 58:
                    raise ValueError("colon_expected")
                index += 1
                values[key] = parse(pointer + "/" + key.replace("~", "~0").replace("/", "~1"))
                whitespace()
                if data[index] == 125:
                    index += 1
                    break
                if data[index] != 44:
                    raise ValueError("comma_expected")
                index += 1
            if duplicate:
                for n in range(first_node, len(nodes)):
                    nodes[n] = Node(nodes[n].pointer, nodes[n].start, nodes[n].end, nodes[n].raw,
                                    nodes[n].value, True)
            return values
        if token == 91:
            index += 1
            values: list[Any] = []
            whitespace()
            if index < len(data) and data[index] == 93:
                index += 1
                return values
            count = 0
            while True:
                values.append(parse(f"{pointer}/{count}"))
                count += 1
                whitespace()
                if data[index] == 93:
                    index += 1
                    break
                if data[index] != 44:
                    raise ValueError("array_comma_expected")
                index += 1
            return values
        start = index
        if token == 34:
            index = string_end(index)
            value = std_json.loads(data[start:index].decode("utf-8"))
            typed = TypedValue("string", value, native=data[start:index].decode("utf-8"))
        else:
            while index < len(data) and data[index] not in b" \t\r\n,]}":
                index += 1
            raw = data[start:index]
            value = std_json.loads(raw.decode("ascii"), parse_float=Decimal, parse_int=int,
                                   parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_constant")))
            if value is None:
                typed = TypedValue("null", None, native=raw.decode("ascii"))
            elif isinstance(value, bool):
                typed = TypedValue("boolean", value, native=raw.decode("ascii"))
            elif isinstance(value, int):
                typed = TypedValue("integer", value, native=raw.decode("ascii"))
            elif isinstance(value, Decimal):
                typed = TypedValue("decimal", value, native=raw.decode("ascii"))
            else:
                raise ValueError("unsupported_scalar")
        nodes.append(Node(pointer, start, index, data[start:index], typed))
        return value

    parse("")
    whitespace()
    if index != len(data):
        raise ValueError("trailing_data")
    return nodes


def _csv_target(target_id: str) -> tuple[int, int]:
    prefix, row, column = target_id.split(":")
    if prefix != "csv":
        raise ValueError("invalid_target")
    return int(row), int(column)


def _csv_typed(value: str, raw: bytes, quoted: bool) -> TypedValue:
    if not quoted and re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value):
        if "." not in value:
            return TypedValue("integer", int(value), native=value)
        return TypedValue("decimal", Decimal(value), native=value)
    return TypedValue("string", value, native=value)


def _matches_typed(actual: TypedValue, expected: TypedValue) -> bool:
    if actual.type != expected.type:
        return False
    if actual.type in {"integer", "decimal"}:
        return Decimal(str(actual.value)) == Decimal(str(expected.value))
    return actual.value == expected.value


def _without_spans(data: bytes, spans: list[tuple[int, int]]) -> bytes:
    output = bytearray()
    cursor = 0
    for start, end in sorted(spans):
        if start < cursor or end < start:
            return b"\x00-overlapping-spans"
        output.extend(data[cursor:start])
        cursor = end
    output.extend(data[cursor:])
    return bytes(output)


def _plan_hash(plan: PatchPlan) -> str:
    import json as std_json

    if not plan.patches:
        value = {"source_sha256": plan.source_sha256, "patches": [], "provenance": plan.provenance}
    else:
        value = {
            "source_sha256": plan.source_sha256,
            "patches": [{"source_version_id": item.source_version_id, "target_id": item.target_id,
                         "expected_old_value": item.expected_old_value.canonical(),
                         "expected_fingerprint": item.expected_fingerprint,
                         "new_value": item.new_value.canonical()} for item in plan.patches],
            "permissions": [asdict(item) for item in plan.permitted_changes],
            "provenance": plan.provenance,
            "dependents": list(plan.dependents),
            "dependencies_unknown": plan.dependencies_unknown,
            "no_change": plan.no_change,
        }
    return hashlib.sha256(std_json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _report(source_hash: str, output_hash: str, checks: list[CheckResult]) -> VerifyReport:
    failed = any(item.status == "FAIL" for item in checks)
    critical_unknown = any(item.status == "NOT_VERIFIED" and item.critical for item in checks)
    unknown = any(item.status == "NOT_VERIFIED" for item in checks)
    outcome = "BLOCKED" if failed or critical_unknown else "INCOMPLETE" if unknown else "VERIFIED"
    return VerifyReport(tuple(checks), outcome, source_hash, output_hash)


def _blocked(source_hash: str, output_hash: str, check: str, reason: str) -> VerifyReport:
    return VerifyReport((CheckResult(check, "FAIL", True, {"reason": reason}),), "BLOCKED", source_hash, output_hash)
