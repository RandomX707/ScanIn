"""Phase 2B: the independent CSV verifier's read-back must equal the legacy (f6139a6) verifier scan."""
from __future__ import annotations

import random

import pytest

from packages.core.verification.text_adapters import _csv_document
from tests.unit._legacy_csv import legacy_verifier_csv_document
from tests.unit.test_csv_compact_parser import FIXTURES, HANDWRITTEN

EXTRA = [b'a,b\n"x"y,z\n', b'a,b\nx"y,z\n', b'a,b\n"ab""', b'a,"b""', b'"', b'""', b'"""', b'a,""""\n',
         b'a,b\n"' + b"x" * 140000 + b'"\n', b"a,b\n" + b"y" * 140000 + b"\n", b"a,b\n" + b"y" * 131072 + b"\n"]


def _run(function, source, *args):
    try:
        return ("ok", function(source, *args))
    except Exception as error:  # the verifier turns any read-back failure into BLOCKED
        return ("error", type(error).__name__)


@pytest.mark.parametrize("source", HANDWRITTEN + EXTRA + [p.read_bytes() for p in FIXTURES],
                         ids=[f"case{i}" for i in range(len(HANDWRITTEN) + len(EXTRA))] + [p.name for p in FIXTURES])
def test_verifier_read_back_matches_legacy(source):
    legacy = _run(legacy_verifier_csv_document, source)
    assert _run(_csv_document, source) == legacy
    if legacy[0] == "ok" and legacy[1]:
        keys = list(legacy[1])
        wanted = frozenset(keys[::3] + [(999999, 1)])
        assert _run(_csv_document, source, wanted) == ("ok", {k: legacy[1][k] for k in keys if k in wanted})


def test_verifier_read_back_matches_legacy_on_random_inputs():
    rng = random.Random(4242)
    alphabet = [b"a", b"1", b" ", b",", b",", b'"', b'"', b'""', b"\n", b"\r\n", b"\r", b";", b"\t",
                "é".encode(), b"\xe9", b"\x00"]
    for _ in range(20000):
        source = b"".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        if rng.random() < 0.2:
            source = b"\xef\xbb\xbf" + source
        legacy = _run(legacy_verifier_csv_document, source)
        assert _run(_csv_document, source) == legacy, source
        if legacy[0] == "ok" and legacy[1]:
            wanted = frozenset(list(legacy[1])[-2:])
            assert _run(_csv_document, source, wanted) == ("ok", {k: v for k, v in legacy[1].items() if k in wanted}), source
