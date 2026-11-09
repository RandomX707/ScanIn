from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from packages.core.adapters.csv_adapter.adapter import CsvAdapter
from packages.core.models.contracts import Patch, TypedValue
from packages.workflow.frozen_serialization import deserialize_frozen_edit_set, serialize_frozen_edit_set
from packages.workflow.release import NativeReleaseWorkflow
from packages.core.verification.text_adapters import CsvIndependentVerifier


def _frozen():
    source = b"claim_id,amount\nCLM-1,125\n"
    target = next(item for item in CsvAdapter().targets(source) if item.id == "csv:2:2")
    patch = Patch(target.version_id, target.id, target.value, target.fingerprint, TypedValue("decimal", Decimal("130.25")))
    return NativeReleaseWorkflow(CsvAdapter(), CsvIndependentVerifier()).freeze(
        source=source, document_id="doc", base_version_id="version", actor_id="processor",
        role="OWNER", authorized_category="insurer-authored", reason="Corrected amount", patches=[patch])


def test_frozen_edit_set_round_trips_with_identical_hashes():
    frozen = _frozen()
    restored = deserialize_frozen_edit_set(serialize_frozen_edit_set(frozen))
    assert restored == frozen


def test_frozen_edit_set_serialization_rejects_plan_tampering():
    payload = serialize_frozen_edit_set(_frozen())
    payload["plan"]["patches"][0]["new_value"]["value"] = "999"
    with pytest.raises(ValueError, match="frozen_plan_integrity_failed"):
        deserialize_frozen_edit_set(payload)
