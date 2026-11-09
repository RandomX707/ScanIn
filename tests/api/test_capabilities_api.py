"""Phase 3A item 2: viewable, natively editable and sidecar-correctable are separate capabilities."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.api.documents import _capabilities


def _report(**overrides):
    base = dict(status="inspected", adapter="pdf", capability={"level": "editable", "reasons": []},
                details={"pages": [{"page": 1, "text": "Invoice", "scanned_or_image_only": False}], "fields": []},
                targets=(), targets_page=None)
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("report,expected", [
    (_report(), (True, True, True)),
    (_report(capability={"level": "Read-only", "reasons": ["no_acroform_fields"]}), (True, False, True)),
    (_report(capability={"level": "Read-only", "reasons": ["signed_pdf"]}), (True, False, True)),
    (_report(capability={"level": "Read-only", "reasons": ["no_acroform_fields"]},
             details={"pages": [{"page": 1, "text": "", "scanned_or_image_only": True}], "fields": []}), (True, False, False)),
    (_report(status="identified", adapter="office", capability={"level": "Read-only", "reasons": ["legacy_office_unsupported"]},
             details=None), (False, False, False)),
    (_report(status="rejected", capability={"level": "rejected", "reasons": ["encrypted_pdf"]}, details=None), (False, False, False)),
    (_report(adapter="xlsx", capability={"level": "Read-only", "reasons": ["macros_present"]}, details=None,
             targets_page={"total": 15}), (True, False, True)),
    (_report(adapter="docx", capability={"level": "Read-only", "reasons": ["tracked_changes_present"]},
             details={"blocks": [{"id": "word/document.xml:p0", "text": "x"}]}), (True, False, True)),
])
def test_capabilities_are_independent(report, expected):
    flags = _capabilities(report)
    assert (flags["viewable"]["available"], flags["native_editing"]["available"], flags["data_corrections"]["available"]) == expected
    for flag in flags.values():
        assert (flag["reason"] is None) == flag["available"]


def test_capability_reasons_name_the_actual_cause():
    legacy = _capabilities(_report(status="identified", adapter="office", details=None,
                                   capability={"level": "Read-only", "reasons": ["legacy_office_unsupported"]}))
    assert legacy["viewable"]["reason"] == legacy["native_editing"]["reason"] == "legacy_office_unsupported"
    signed = _capabilities(_report(capability={"level": "Read-only", "reasons": ["signed_pdf"]}))
    assert signed["native_editing"]["reason"] == "signed_pdf" and signed["viewable"]["reason"] is None
    scanned = _capabilities(_report(capability={"level": "Read-only", "reasons": ["no_acroform_fields"]},
                                    details={"pages": [{"page": 1, "text": " ", "scanned_or_image_only": True}], "fields": []}))
    assert scanned["data_corrections"]["reason"] == "no_extractable_text_or_fields"
