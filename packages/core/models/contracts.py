from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal


ValueType = Literal["string", "integer", "decimal", "date", "datetime", "time", "boolean", "choice", "null"]


@dataclass(frozen=True)
class TypedValue:
    type: ValueType
    value: str | int | float | Decimal | bool | None
    native: str | None = None
    display: str | None = None

    def __post_init__(self) -> None:
        valid = {
            "string": isinstance(self.value, str),
            "choice": isinstance(self.value, str),
            "integer": isinstance(self.value, int) and not isinstance(self.value, bool),
            "decimal": isinstance(self.value, (int, float, Decimal)) and not isinstance(self.value, bool),
            "date": isinstance(self.value, str),
            "datetime": isinstance(self.value, str),
            # Time of day without a date (e.g. a time-only spreadsheet cell); never carries a timezone.
            "time": isinstance(self.value, str),
            "boolean": isinstance(self.value, bool),
            "null": self.value is None,
        }
        if not valid[self.type]:
            raise ValueError(f"invalid_value_type:{self.type}")
        if isinstance(self.value, float) and (self.value != self.value or abs(self.value) == float("inf")):
            raise ValueError("non_finite_number")
        if isinstance(self.value, Decimal) and not self.value.is_finite():
            raise ValueError("non_finite_number")

    def canonical(self) -> dict[str, Any]:
        return {"type": self.type, "value": str(self.value) if isinstance(self.value, Decimal) else self.value,
                "native": self.native, "display": self.display}


@dataclass(frozen=True)
class Patch:
    source_version_id: str
    target_id: str
    expected_old_value: TypedValue
    expected_fingerprint: str
    new_value: TypedValue


@dataclass(frozen=True)
class EditTarget:
    id: str
    version_id: str
    sheet: str
    cell: str
    value: TypedValue
    fingerprint: str
    style_id: str | None
    editable: bool = True
    reason: str | None = None
    dependents: tuple[str, ...] | None = ()
    label: str | None = None


@dataclass(frozen=True)
class CapabilityProfile:
    level: str
    reasons: tuple[str, ...]
    features: tuple[str, ...]
    sheets: tuple[str, ...]
    adapter: str = "xlsx"
    adapter_version: str = "0.1.0"


@dataclass(frozen=True)
class ChangePermission:
    part: str
    locator: str | None
    operation: str
    old_fingerprint: str | None
    expected_new_fingerprint: str


@dataclass(frozen=True)
class PatchPlan:
    source_version_id: str
    source_sha256: str
    patches: tuple[Patch, ...]
    permitted_changes: tuple[ChangePermission, ...]
    provenance: tuple[tuple[str, str], ...]
    dependents: tuple[str, ...]
    dependencies_unknown: bool
    no_change: bool
    plan_sha256: str

    def provenance_dict(self) -> dict[str, str]:
        return dict(self.provenance)


@dataclass(frozen=True)
class CheckResult:
    check: str
    status: Literal["PASS", "FAIL", "NOT_VERIFIED"]
    critical: bool
    detail: dict[str, Any]


@dataclass(frozen=True)
class VerifyReport:
    checks: tuple[CheckResult, ...]
    outcome: Literal["VERIFIED", "STRUCTURALLY_VERIFIED", "INCOMPLETE", "BLOCKED"]
    source_sha256: str
    output_sha256: str
    verification_profile: str | None = None
    structural_checks: str | None = None
    visual_checks: str | None = None

    def check(self, name: str) -> CheckResult:
        return next(c for c in self.checks if c.check == name)

    def as_dict(self) -> dict[str, Any]:
        result = {
            "outcome": self.outcome,
            "source_sha256": self.source_sha256,
            "output_sha256": self.output_sha256,
            "checks": [{"check": c.check, "status": c.status, "critical": c.critical, "detail": c.detail} for c in self.checks],
        }
        if self.verification_profile is not None:
            result.update({"verification_profile": self.verification_profile,
                           "structural_checks": self.structural_checks,
                           "visual_checks": self.visual_checks})
        return result
