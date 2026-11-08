from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from packages.core.models.contracts import CapabilityProfile, ChangePermission, EditTarget, PatchPlan, TypedValue


class JsonError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class JsonScalar:
    pointer: str
    start: int
    end: int
    raw: bytes
    value: TypedValue
    duplicate_ancestor: bool = False


@dataclass(frozen=True)
class JsonDocument:
    scalars: tuple[JsonScalar, ...]
    escape_unicode: bool
    value: Any


class _Tokenizer:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.scalars: list[JsonScalar] = []

    def parse(self) -> JsonDocument:
        self._space()
        value = self._value("")
        self._space()
        if self.pos != len(self.data):
            raise JsonError("invalid_json")
        try:
            decoded = self.data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise JsonError("non_utf8_json") from None
        escape_style = bool(re.search(r"\\u[0-9a-fA-F]{4}", decoded))
        return JsonDocument(tuple(self.scalars), escape_style, value)

    def _value(self, pointer: str) -> Any:
        self._space()
        if self.pos >= len(self.data):
            raise JsonError("invalid_json")
        byte = self.data[self.pos]
        if byte == 123:
            return self._object(pointer)
        if byte == 91:
            return self._array(pointer)
        start = self.pos
        if byte == 34:
            self.pos = self._string_end(self.pos)
            value = self._loads(self.data[start:self.pos])
            self.scalars.append(JsonScalar(pointer, start, self.pos, self.data[start:self.pos], TypedValue("string", value, native=self.data[start:self.pos].decode("utf-8"))))
            return value
        while self.pos < len(self.data) and self.data[self.pos] not in b" \t\r\n,]}:":
            self.pos += 1
        if self.pos == start:
            raise JsonError("invalid_json")
        raw = self.data[start:self.pos]
        value = self._loads(raw)
        typed = self._typed(value, raw)
        self.scalars.append(JsonScalar(pointer, start, self.pos, raw, typed))
        return value

    def _object(self, pointer: str) -> dict[str, Any]:
        self.pos += 1
        self._space()
        pairs: list[tuple[str, Any]] = []
        keys: set[str] = set()
        duplicate = False
        scalar_start = len(self.scalars)
        if self._take(125):
            return {}
        while True:
            self._space()
            if self.pos >= len(self.data) or self.data[self.pos] != 34:
                raise JsonError("invalid_json")
            start = self.pos
            self.pos = self._string_end(self.pos)
            key = self._loads(self.data[start:self.pos])
            if not isinstance(key, str):
                raise JsonError("invalid_json")
            duplicate |= key in keys
            keys.add(key)
            self._space()
            if not self._take(58):
                raise JsonError("invalid_json")
            child_pointer = f"{pointer}/{self._escape_pointer(key)}"
            value = self._value(child_pointer)
            pairs.append((key, value))
            self._space()
            if self._take(125):
                break
            if not self._take(44):
                raise JsonError("invalid_json")
        if duplicate:
            for index in range(scalar_start, len(self.scalars)):
                self.scalars[index] = replace(self.scalars[index], duplicate_ancestor=True)
        # Keep a deterministic last-value representation, matching common JSON readers.
        result: dict[str, Any] = {}
        for key, value in pairs:
            result[key] = value
        return result

    def _array(self, pointer: str) -> list[Any]:
        self.pos += 1
        self._space()
        values: list[Any] = []
        if self._take(93):
            return values
        index = 0
        while True:
            values.append(self._value(f"{pointer}/{index}"))
            index += 1
            self._space()
            if self._take(93):
                break
            if not self._take(44):
                raise JsonError("invalid_json")
        return values

    def _string_end(self, start: int) -> int:
        pos = start + 1
        while pos < len(self.data):
            byte = self.data[pos]
            if byte == 92:
                pos += 2
                continue
            if byte == 34:
                return pos + 1
            if byte < 32:
                raise JsonError("invalid_json")
            pos += 1
        raise JsonError("invalid_json")

    def _loads(self, raw: bytes) -> Any:
        try:
            return json.loads(raw.decode("utf-8"), parse_float=Decimal, parse_int=int,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_constant")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise JsonError("invalid_json") from None

    @staticmethod
    def _typed(value: Any, raw: bytes) -> TypedValue:
        if value is None:
            return TypedValue("null", None, native=raw.decode("ascii"))
        if isinstance(value, bool):
            return TypedValue("boolean", value, native=raw.decode("ascii"))
        if isinstance(value, int):
            return TypedValue("integer", value, native=raw.decode("ascii"))
        if isinstance(value, Decimal):
            return TypedValue("decimal", value, native=raw.decode("ascii"))
        raise JsonError("unsupported_json_scalar")

    def _space(self) -> None:
        while self.pos < len(self.data) and self.data[self.pos] in b" \t\r\n":
            self.pos += 1

    def _take(self, byte: int) -> bool:
        if self.pos < len(self.data) and self.data[self.pos] == byte:
            self.pos += 1
            return True
        return False

    @staticmethod
    def _escape_pointer(value: str) -> str:
        return value.replace("~", "~0").replace("/", "~1")


class JsonAdapter:
    name = "json"
    version = "0.1.0"

    def inspect(self, source: bytes) -> CapabilityProfile:
        try:
            self._parse(source)
        except JsonError as error:
            level = "read_only" if error.code == "non_utf8_json" else "rejected"
            return CapabilityProfile(level, (error.code,), (), (), self.name, self.version)
        return CapabilityProfile("editable", (), ("utf8", "byte_spans"), (), self.name, self.version)

    def targets(self, source: bytes) -> list[EditTarget]:
        doc = self._parse(source)
        version = hashlib.sha256(source).hexdigest()
        return [EditTarget(self._target_id(scalar.pointer), version, "", scalar.pointer, scalar.value,
                           hashlib.sha256(scalar.raw).hexdigest(), None, True, None, (), scalar.pointer.rsplit("/", 1)[-1] or "root")
                for scalar in doc.scalars if not scalar.duplicate_ancestor]

    def unsupported_targets(self, source: bytes) -> list[EditTarget]:
        doc = self._parse(source)
        version = hashlib.sha256(source).hexdigest()
        return [EditTarget(self._target_id(scalar.pointer), version, "", scalar.pointer, scalar.value,
                           hashlib.sha256(scalar.raw).hexdigest(), None, False, "duplicate_keys", (),
                           scalar.pointer.rsplit("/", 1)[-1] or "root")
                for scalar in doc.scalars if scalar.duplicate_ancestor]

    def target_at(self, source: bytes, selection: dict[str, Any]) -> EditTarget | None:
        if not isinstance(selection.get("pointer"), str):
            return None
        target_id = self._target_id(selection["pointer"])
        return next((item for item in self.targets(source) if item.id == target_id), None)

    def validate_new_value(self, value: TypedValue) -> None:
        if value.type not in {"string", "integer", "decimal", "date", "datetime", "boolean", "null"}:
            raise JsonError("invalid_type")
        if value.type == "decimal" and not Decimal(str(value.value)).is_finite():
            raise JsonError("invalid_number")
        if value.type == "date":
            try:
                date.fromisoformat(str(value.value))
            except ValueError:
                raise JsonError("invalid_date") from None
        if value.type == "datetime":
            try:
                datetime.fromisoformat(str(value.value))
            except ValueError:
                raise JsonError("invalid_datetime") from None

    def formula_dependents(self, source: bytes, target: EditTarget) -> tuple[tuple[str, ...], bool]:
        return (), False

    @staticmethod
    def type_compatible(old: TypedValue, new: TypedValue) -> bool:
        return True

    def permission_for(self, source: bytes, target: EditTarget, value: TypedValue,
                       provenance: dict[str, str]) -> tuple[ChangePermission, ...]:
        self.validate_new_value(value)
        scalar = self._target_scalar(self._parse(source), target.id)
        new_raw = self._serialize(value, self._parse(source).escape_unicode)
        return (ChangePermission("body", target.id, "replace_value", target.fingerprint,
                                 hashlib.sha256(new_raw).hexdigest()),)

    def apply(self, source: bytes, plan: PatchPlan) -> bytes | None:
        if plan.no_change:
            return None
        if hashlib.sha256(source).hexdigest() != plan.source_sha256:
            raise JsonError("stale_source")
        doc = self._parse(source)
        replacements: list[tuple[int, int, bytes]] = []
        for patch in plan.patches:
            scalar = self._target_scalar(doc, patch.target_id)
            if scalar.duplicate_ancestor:
                raise JsonError("duplicate_keys")
            if hashlib.sha256(scalar.raw).hexdigest() != patch.expected_fingerprint:
                raise JsonError("fingerprint_mismatch")
            self.validate_new_value(patch.new_value)
            replacements.append((scalar.start, scalar.end, self._serialize(patch.new_value, doc.escape_unicode)))
        output = bytearray(source)
        for start, end, encoded in sorted(replacements, reverse=True):
            output[start:end] = encoded
        return bytes(output)

    def parse(self, source: bytes) -> Any:
        return self._parse(source).value

    @staticmethod
    def _parse(source: bytes) -> JsonDocument:
        if not source:
            raise JsonError("empty_json")
        try:
            source.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise JsonError("non_utf8_json") from None
        return _Tokenizer(source).parse()

    @staticmethod
    def _target_id(pointer: str) -> str:
        return f"json:{pointer}"

    def _target_scalar(self, doc: JsonDocument, target_id: str) -> JsonScalar:
        pointer = target_id[5:] if target_id.startswith("json:") else None
        if pointer is None:
            raise JsonError("invalid_target_id")
        scalar = next((item for item in doc.scalars if item.pointer == pointer), None)
        if scalar is None:
            raise JsonError("target_not_found")
        return scalar

    @staticmethod
    def _serialize(value: TypedValue, escape_unicode: bool) -> bytes:
        raw = value.value
        if value.type == "decimal":
            text = str(Decimal(str(raw)))
            if not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", text):
                raise JsonError("invalid_number")
            return text.encode("ascii")
        if value.type == "date" or value.type == "datetime":
            raw = str(raw)
        try:
            return json.dumps(raw, ensure_ascii=escape_unicode, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            raise JsonError("invalid_value") from None
