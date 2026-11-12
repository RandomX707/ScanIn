"""Structural classification of PDF digital signatures (no cryptographic trust claims).

This looks at the signature *structures* (/ByteRange, /Sig fields, DocMDP) with bounded byte-pattern scans.
It never says a signature is "valid" or "trusted": cryptographic validation is a separate capability that is
reported as `not_validated` until a real validator is wired in.
"""
from __future__ import annotations

import re
from typing import Any

_BYTERANGE = re.compile(rb"/ByteRange\s*\[\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*\]")
_SIG_FIELD = re.compile(rb"/FT\s*/Sig")
_SIG_DICT = re.compile(rb"/Type\s*/Sig\b")
_CONTENTS = re.compile(rb"/Contents\s*<[0-9A-Fa-f]{20,}")
MAX_SCAN = 80 * 1024 * 1024


def classify(data: bytes) -> dict[str, Any]:
    """status: none | signed | signature_range_mismatch | modified_after_signing | unsigned_signature_field"""
    if len(data) > MAX_SCAN:
        return {"status": "unknown", "signatures": [], "validated": False}
    size = len(data)
    sigs: list[dict[str, Any]] = []
    for m in _BYTERANGE.finditer(data):
        a, b, c, d = (int(x) for x in m.groups())
        in_bounds = a == 0 and b > 0 and c >= b and c + d <= size
        # PDFs may carry one or two trailing line-break bytes after the last signed byte.
        covers = in_bounds and size - (c + d) <= 2
        sigs.append({"byte_range": [a, b, c, d], "in_bounds": in_bounds, "covers_whole_file": covers,
                     "signed_to": c + d, "file_size": size})
    has_field = bool(_SIG_FIELD.search(data))
    has_contents = bool(_CONTENTS.search(data)) and bool(_SIG_DICT.search(data))
    if sigs:
        if any(not s["in_bounds"] for s in sigs):
            status = "signature_range_mismatch"
        elif any(not s["covers_whole_file"] for s in sigs):
            status = "modified_after_signing"
        else:
            status = "signed"
    elif has_field and has_contents:
        status = "signature_range_mismatch"      # signature value present but no usable range
    elif has_field:
        status = "unsigned_signature_field"
    else:
        status = "none"
    return {"status": status, "signatures": sigs, "has_signature_field": has_field,
            "docmdp": b"/DocMDP" in data, "validated": False, "trust": "not_validated"}


REASONS = {
    "signed": "This PDF is digitally signed. Editing it would break the signature, so it stays read-only and the original is kept exactly as issued.",
    "signature_range_mismatch": "This PDF carries a digital signature whose protected range doesn't match the file, so its authenticity can't be confirmed. It stays read-only and the original is kept exactly as received.",
    "modified_after_signing": "This PDF was changed after it was signed, so the signature no longer covers the whole file. It stays read-only and the original is kept exactly as received.",
}


def blocks_editing(status: str) -> bool:
    return status in REASONS
