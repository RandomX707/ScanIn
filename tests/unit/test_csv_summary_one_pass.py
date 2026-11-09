"""Phase 2B: the one-pass CSV structural summary must equal the legacy (f6139a6) quadratic summary."""
from __future__ import annotations

import random

import pytest

from packages.core.adapters.csv_adapter.adapter import CsvAdapter
from tests.unit.test_csv_compact_parser import FIXTURES, HANDWRITTEN


SUMMARY_CASES = HANDWRITTEN + [
    b"claim_number,policy_number,insured_name\nC1,P1,Asha\n,,\n  , \t,\nC2,P2\n",
    b'claim_number,,status\nC1,"",open\n"","  ",""\n',
    b'h1,h2\n"line\nbreak",2\n\n\n',
    b"1,2,3\n4,5,6\n",                    # no header (data-like first row)
    b"policyholder,premium\nA,\xc2\xa0\n\xc2\xa0,\xc2\xa0\n",   # NBSP-only cells are blank after strip()
    b"policyholder,premium\r\nA,\xa0\r\n\xa0,\xa0\r\n",       # cp1252 NBSP
]


def _legacy_summary(source: bytes):
    """Legacy CSV facts with the provider's unchanged final post-processing applied."""
    from packages.core.summaries.structural import _limit_words
    from tests.unit._legacy_csv import legacy_csv_summary_facts
    facts = legacy_csv_summary_facts(source)
    return {**facts, "description": _limit_words(facts["description"], 100),
            "observed": facts["observed"][:20], "evidence": facts["evidence"][:30]}


def _summary_facts(source: bytes):
    from packages.core.summaries.structural import StructuralSummaryProvider
    summary = StructuralSummaryProvider().summarize(source=source, adapter="csv", details=None)
    return {"stats": summary["statistics"], "observed": summary["observed_fields"],
            "evidence": summary["evidence_references"], "description": summary["description"],
            "apparent_type": summary["apparent_document_type"], "limitations": summary["limitations"]}


@pytest.mark.parametrize("source", SUMMARY_CASES + [p.read_bytes() for p in FIXTURES],
                         ids=[f"case{i}" for i in range(len(SUMMARY_CASES))] + [p.name for p in FIXTURES])
def test_structural_summary_matches_legacy_summary(source):
    try:
        legacy = _legacy_summary(source)
    except (ValueError, UnicodeError, KeyError, TypeError, OSError):
        legacy = None
    new = _summary_facts(source)
    if legacy is None:
        assert new["stats"] == {} and new["description"].endswith("Its purpose is unclear.")
        return
    assert new["stats"] == legacy["stats"]
    assert new["observed"] == legacy["observed"] and new["evidence"] == legacy["evidence"]
    assert new["description"] == legacy["description"]
    assert new["apparent_type"] == legacy["apparent_type"]
    assert legacy["limitations"] == [item for item in new["limitations"] if item in legacy["limitations"]]


def test_structural_summary_matches_legacy_on_random_inputs():
    rng = random.Random(7)
    alphabet = [b"claim", b"_number", b"policy", b"x", b"1", b" ", b"\t", b",", b",", b'"', b'""', b"\n", b"\r\n",
                " ".encode(), b"Rao"]
    for _ in range(3000):
        source = b"".join(rng.choice(alphabet) for _ in range(rng.randint(1, 30)))
        try:
            legacy = _legacy_summary(source)
        except (ValueError, UnicodeError, KeyError, TypeError, OSError):
            continue
        new = _summary_facts(source)
        assert (new["stats"], new["description"], new["observed"]) == (legacy["stats"], legacy["description"], legacy["observed"]), source


def test_structural_summary_reuses_the_request_document():
    from packages.core.summaries.structural import StructuralSummaryProvider
    source = b"claim_number,insured_name\nC1,Asha\n"
    adapter = CsvAdapter()
    document = adapter.document(source)
    calls = []
    original = CsvAdapter.document
    try:
        CsvAdapter.document = lambda self, data: calls.append(data) or original(self, data)
        summary = StructuralSummaryProvider().summarize(source=source, adapter="csv", details=None, parsed=document)
    finally:
        CsvAdapter.document = original
    assert calls == [] and summary["statistics"] == {"record_count": 1, "column_count": 2}
