"""Acceptance tests on the PRIVATE stakeholder PDFs (gitignored). Skipped, never faked, when they are absent."""
import os
import random
from pathlib import Path

import pytest

from packages.core import pdf_signature
from packages.workers.pdf_edit_entry import apply_edits, list_runs, verify

ROOT = Path(__file__).resolve().parents[2]
CORPUS = Path(os.environ.get("CHANNEL360_CORPUS_DIR", ROOT / "Policy Copies"))
QCR = ROOT / ".private/corpus/QCR/QCR/QCR Health.pdf"

EDITABLE = [("CAR Policy copy.pdf", [1, 2, 5]), ("Fire Policy copy.pdf", [3, 20, 50]), ("GMC-Policy copy.pdf", [1, 2, 3]),
            ("Marine Policy copy.pdf", [1, 4])]
SIGNED = {"Health Policy copy.pdf": "signed", "Motor  Policy.pdf": "signed", "Marine Policy copy.pdf": "signature_range_mismatch"}


def _path(name):
    p = CORPUS / name
    if not p.exists():
        pytest.skip("private stakeholder corpus not present")
    return p


@pytest.mark.parametrize("name,expected", sorted(SIGNED.items()))
def test_signature_classification(name, expected):
    assert pdf_signature.classify(_path(name).read_bytes())["status"] == expected


@pytest.mark.parametrize("name", ["CAR Policy copy.pdf", "Fire Policy copy.pdf", "GMC-Policy copy.pdf", "QCR/QCR Health.pdf"])
def test_unsigned_policies_have_no_signature(name):
    path = QCR if name.startswith("QCR/") else _path(name)
    if not path.exists():
        pytest.skip("private stakeholder corpus not present")
    assert pdf_signature.classify(path.read_bytes())["status"] == "none"


@pytest.mark.parametrize("name,pages", EDITABLE + [("QCR", [1, 2])])
def test_real_text_edits_verify(name, pages):
    path = QCR if name == "QCR" else _path(name)
    if not path.exists():
        pytest.skip("private stakeholder corpus not present")
    src = path.read_bytes()
    rng = random.Random(5)
    done = 0
    for page in pages:
        runs = [r for r in list_runs(src, page)["runs"] if r["e"] and 5 <= len(r["text"]) <= 40]
        for r in rng.sample(runs, min(4, len(runs))):
            res = apply_edits(src, [{"id": r["id"], "old": r["text"], "new": r["text"][:-2]}])
            assert "error" not in res, res.get("error")
            report = verify(src, res["out"], res["plans"], res["before_runs"])
            failed = [c for c in report["checks"] if c["status"] != "pass"]
            assert report["outcome"] == "VERIFIED", failed
            done += 1
    assert done >= 4


def test_batch_edit_across_pages_and_shared_forms():
    src = _path("GMC-Policy copy.pdf").read_bytes()
    rng = random.Random(9)
    batch = []
    for page in (1, 2, 3, 5):
        runs = [r for r in list_runs(src, page)["runs"] if r["e"] and 6 <= len(r["text"]) <= 30]
        batch += [{"id": r["id"], "old": r["text"], "new": r["text"][:-2]} for r in rng.sample(runs, 3)]
    res = apply_edits(src, batch)
    assert "error" not in res
    report = verify(src, res["out"], res["plans"], res["before_runs"])
    assert report["outcome"] == "VERIFIED", [c for c in report["checks"] if c["status"] != "pass"]


def test_invisible_text_is_never_offered():
    src = _path("GMC-Policy copy.pdf").read_bytes()
    runs = list_runs(src, 1)["runs"]
    assert 0 < len(runs) < 374          # the tiled export holds 374 runs per page, most of them clipped away
