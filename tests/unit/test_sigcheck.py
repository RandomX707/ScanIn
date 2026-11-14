"""PDF signature validation with a REAL (self-signed, synthetic) CMS signature: integrity, trust and coverage stay separate."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from packages.workers.sigcheck import SignatureValidator, _der_length, _extract

validator = SignatureValidator()
needs_openssl = pytest.mark.skipif(not validator.available(), reason="sandboxed OpenSSL 3+ validator not available on this host")
SLOT = 8192


def _openssl(*args: str, cwd: Path, data: bytes | None = None) -> bytes:
    return subprocess.run([validator.openssl, *args], cwd=cwd, input=data, capture_output=True, check=True, env={"PATH": "/usr/bin:/bin", "OPENSSL_CONF": "/dev/null"}).stdout


def make_identity(tmp: Path, cn: str = "Synthetic Test Signer") -> tuple[Path, Path]:
    _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "key.pem", "-out", "cert.pem", "-days", "30", "-subj", f"/CN={cn}/O=Channel360 Tests", cwd=tmp)
    return tmp / "cert.pem", tmp / "key.pem"


def signed_pdf(tmp: Path, cert: Path, key: Path, trailing: bytes = b"") -> bytes:
    """A minimal one-page PDF whose /Contents placeholder is filled with a genuine detached CMS signature over the byte ranges."""
    head = (b"%PDF-1.7\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n4 0 obj<</Type/Sig/Filter/Adobe.PPKLite/SubFilter/adbe.pkcs7.detached/ByteRange")
    placeholder_range = b"[0 0000000000 0000000000 0000000000]"
    mid = b"/Contents<"
    tail = b">>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    zeros = b"0" * (SLOT * 2)
    probe = head + placeholder_range + mid + zeros + tail
    start = probe.index(b"<" + zeros) + 1                       # first hex digit of the placeholder
    a, b = 0, start - 1
    c = start + len(zeros) + 1
    d = len(probe) - c
    rng = f"[0 {b:010d} {c:010d} {d:010d}]".encode()
    assert len(rng) == len(placeholder_range)
    body = head + rng + mid + zeros + tail
    signed = body[:b] + body[c:]
    (tmp / "signed.bin").write_bytes(signed)
    der = _openssl("cms", "-sign", "-binary", "-outform", "DER", "-in", "signed.bin", "-signer", str(cert), "-inkey", str(key), cwd=tmp)
    assert len(der) <= SLOT
    filled = der.hex().upper().encode().ljust(len(zeros), b"0")
    return body[: b + 1] + filled + body[c - 1:] + trailing


@pytest.fixture(scope="module")
def identity(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sig")
    cert, key = make_identity(tmp)
    return tmp, cert, key


def test_der_length_and_extract_reject_malformed_input():
    assert _der_length(bytes.fromhex("3003020101") + b"\x00\x00") == 5
    assert _der_length(b"\x04\x01x") is None
    assert _extract(b"x" * 100, [0, 50, 90, 30])[2] == "byte_range_outside_file"
    data = b"A" * 10 + b"<zz>" + b"B" * 10
    assert _extract(data, [0, 10, 14, 10])[2] == "contents_not_hex_string"


@needs_openssl
def test_genuine_signature_is_intact_but_trust_is_not_established_without_a_policy(identity):
    tmp, cert, key = identity
    result = SignatureValidator(trust_file="").validate(signed_pdf(tmp, cert, key))
    assert result["state"] == "integrity_valid_trust_not_established"
    assert result["validated"] is False and result["trust_policy_configured"] is False
    sig = result["signatures"][0]
    assert sig["integrity"] == "valid" and sig["trust"] == "not_established" and sig["covers_whole_file"] is True
    assert "Synthetic Test Signer" in sig["signer"]["subject"]


@needs_openssl
def test_signer_chaining_to_an_explicit_trust_file_is_trusted_by_policy_only(identity):
    tmp, cert, key = identity
    result = SignatureValidator(trust_file=str(cert)).validate(signed_pdf(tmp, cert, key))
    assert result["state"] == "integrity_valid_trusted" and result["validated"] is True
    assert result["signatures"][0]["trust"] == "trusted_by_policy"
    assert "legal" in " ".join(result["limits"]).lower() or "legal" in result["detail"].lower()


@needs_openssl
def test_a_different_trust_anchor_means_untrusted(identity, tmp_path):
    tmp, cert, key = identity
    other_cert, _ = make_identity(tmp_path, cn="Some Other CA")
    result = SignatureValidator(trust_file=str(other_cert)).validate(signed_pdf(tmp, cert, key))
    assert result["state"] == "integrity_valid_untrusted" and result["validated"] is False


@needs_openssl
def test_one_flipped_byte_in_the_signed_range_breaks_integrity(identity):
    tmp, cert, key = identity
    data = bytearray(signed_pdf(tmp, cert, key))
    data[data.index(b"/Catalog") + 2] ^= 0x01
    result = SignatureValidator(trust_file="").validate(bytes(data))
    assert result["state"] == "integrity_invalid" and result["validated"] is False
    assert result["signatures"][0]["integrity"] == "invalid"


@needs_openssl
def test_content_appended_after_signing_is_flagged_not_called_valid(identity):
    tmp, cert, key = identity
    result = SignatureValidator(trust_file="").validate(signed_pdf(tmp, cert, key, trailing=b"\n1 0 obj<<>>endobj\nxref\n" + b"added later " * 20))
    assert result["state"] == "modified_after_signing"
    assert result["signatures"][0]["covers_whole_file"] is False


@needs_openssl
def test_signature_range_pointing_outside_the_file_is_malformed(identity):
    tmp, cert, key = identity
    data = signed_pdf(tmp, cert, key)
    broken = data.replace(b"/ByteRange[", b"/ByteRange[", 1)
    import re
    broken = re.sub(rb"(/ByteRange\[0 \d{10} \d{10} )(\d{10})", lambda m: m.group(1) + b"%010d" % (len(data) + 999), data, count=1)
    result = SignatureValidator(trust_file="").validate(broken)
    assert result["state"] == "malformed"


def test_no_signature_is_reported_as_none_and_unavailable_validator_never_claims_validity(monkeypatch, identity):
    plain = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
    assert SignatureValidator().validate(plain)["state"] == "none"
    if validator.available():
        tmp, cert, key = identity
        v = SignatureValidator()
        monkeypatch.setattr(SignatureValidator, "available", lambda self: False)
        result = v.validate(signed_pdf(tmp, cert, key))
        assert result["state"] == "not_validated" and result["validated"] is False
