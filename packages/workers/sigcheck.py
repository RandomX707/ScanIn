"""Cryptographic validation of PDF digital signatures (CMS/PKCS#7) with OpenSSL, inside an OS sandbox.

Three separate questions are answered and NEVER merged into one word like "valid":
  1. integrity   - do the signed byte ranges still hash to what the signer signed, and does the signature verify?
  2. trust       - does the signer certificate chain to a CA in an EXPLICITLY configured trust file?
                   (no trust file configured -> "not_established", never "trusted")
  3. coverage    - does the signature cover the whole file, or was content appended after signing?
Legal/regulatory acceptance is outside this module and is never claimed.

Malformed containers (byte ranges outside the file, missing /Contents, unreadable DER) are reported as such and are
never treated as trusted. OpenSSL runs through packages/workers/sandbox.py (no network, writes only in a scratch directory, no read access to
the user's files). Without a verified sandbox, validation is reported as unavailable rather than run unconfined.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from packages.core import pdf_signature
from packages.workers.sandbox import SandboxFailed, SandboxPolicy, SandboxUnavailable, confined_runner, resolve_tool, sandbox_status

MAX_SIGNATURES = 8
OID_TIMESTAMP = bytes.fromhex("2a864886f70d010910020e")      # id-aa-timeStampToken
OID_SIGNING_TIME = bytes.fromhex("2a864886f70d010905")       # signingTime attribute
_CONTENTS_AT = re.compile(rb"/Contents\s*<")
_DOCMDP = re.compile(rb"/TransformMethod\s*/DocMDP.{0,200}?/P\s+(\d)|/P\s+(\d).{0,200}?/TransformMethod\s*/DocMDP", re.S)
_SUBFILTER = re.compile(rb"/SubFilter\s*/([A-Za-z0-9_.]+)")

LABELS = {
    "none": ("No digital signature", "This PDF carries no digital signature."),
    "unsigned_signature_field": ("Empty signature field", "The PDF has a signature field, but nobody has signed it."),
    "malformed": ("Malformed signature · not trusted", "The signature container is damaged or its protected range does not match the file, so it cannot be verified and is not trusted."),
    "integrity_invalid": ("Signature does not verify · not trusted", "The signed content no longer matches the signature. The document was altered after signing, or the signature is not genuine."),
    "not_validated": ("Signature present · could not be validated", "A signature exists, but it could not be checked on this system. It is not trusted."),
    "modified_after_signing": ("Changed after signing", "The signature covers only an earlier part of the file; later content is NOT covered by it."),
    "integrity_valid_trust_not_established": ("Signature intact · signer not trusted", "The signed content is unchanged and the signature verifies cryptographically, but no trust policy is configured here, so the signer's identity is not established."),
    "integrity_valid_untrusted": ("Signature intact · certificate not trusted", "The signature verifies cryptographically, but the signer's certificate does not chain to a trusted authority."),
    "integrity_valid_trusted": ("Signature intact · signer trusted by policy", "The signature verifies and the certificate chains to an authority in the configured trust policy. This is not a statement about legal acceptance."),
}
_SEVERITY = ["malformed", "integrity_invalid", "not_validated", "modified_after_signing", "integrity_valid_untrusted",
             "integrity_valid_trust_not_established", "integrity_valid_trusted"]


def _der_length(blob: bytes) -> int | None:
    """Total length of the leading DER SEQUENCE, so the zero padding after it can be dropped."""
    if len(blob) < 2 or blob[0] != 0x30:
        return None
    first = blob[1]
    if first < 0x80:
        total = 2 + first
    else:
        n = first & 0x7F
        if n == 0 or n > 4 or len(blob) < 2 + n:
            return None
        total = 2 + n + int.from_bytes(blob[2:2 + n], "big")
    return total if total <= len(blob) else None


def _extract(data: bytes, rng: list[int]) -> tuple[bytes | None, bytes | None, str | None]:
    a, b, c, d = rng
    if not (a == 0 and 0 < b <= c and c + d <= len(data)):
        return None, None, "byte_range_outside_file"
    gap = data[a + b:c]
    if not (gap[:1] == b"<" and gap[-1:] == b">"):
        return None, None, "contents_not_hex_string"
    hex_text = re.sub(rb"\s+", b"", gap[1:-1])
    if len(hex_text) % 2 or not re.fullmatch(rb"[0-9A-Fa-f]*", hex_text):
        return None, None, "contents_not_hex_string"
    raw = bytes.fromhex(hex_text.decode())
    length = _der_length(raw)
    if length is None:
        if raw[:2] == b"\x30\x80":                  # BER indefinite length (legal in PDF signatures): hand OpenSSL the whole container
            return raw, data[a:a + b] + data[c:c + d], None
        return None, None, "signature_not_der"
    return raw[:length], data[a:a + b] + data[c:c + d], None


class _Proc:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes):
        self.returncode, self.stdout, self.stderr = returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


class SignatureValidator:
    def __init__(self, *, trust_file: str | None = None, timeout_seconds: int = 20, runner=None):
        configured = trust_file or os.environ.get("IDW_PDF_TRUST_CAFILE")
        self.trust_file = Path(configured) if configured and Path(configured).is_file() else None
        self.timeout_seconds = timeout_seconds
        self._runner = runner
        self.tool = resolve_tool("openssl")
        self.openssl = self.tool.path if self.tool else None

    def available(self) -> bool:
        if self.tool is None:
            return False
        return self._runner is not None or sandbox_status().verified_here

    def _run(self, scratch: Path, args: list[str]) -> _Proc:
        runner = self._runner or confined_runner()
        policy = SandboxPolicy(scratch=scratch, read_paths=((self.trust_file,) if self.trust_file else ()), timeout_seconds=self.timeout_seconds,
                               cpu_seconds=self.timeout_seconds * 2, memory_bytes=1024**3, max_open_files=256)
        result = runner.run(self.tool, args, policy, capture_stdout=True, capture_stderr=True)
        return _Proc(result.returncode, result.stdout, result.stderr)

    def validate(self, data: bytes) -> dict[str, Any]:
        structure = pdf_signature.classify(data)
        result: dict[str, Any] = {"structure": structure["status"], "signatures": [], "docmdp": None, "trust_policy_configured": self.trust_file is not None,
                                  "limits": ["Cryptographic integrity, certificate trust and legal acceptance are different things; this check never claims legal acceptance."]}
        mdp = _DOCMDP.search(data[:2_000_000])
        if mdp:
            level = int((mdp.group(1) or mdp.group(2)).decode())
            result["docmdp"] = {"permission": level, "meaning": {1: "no changes allowed", 2: "form filling and signing allowed", 3: "form filling, signing and annotations allowed"}.get(level, "unknown")}
        sigs = structure.get("signatures", [])[:MAX_SIGNATURES]
        if not sigs:
            state = "unsigned_signature_field" if structure["status"] == "unsigned_signature_field" else "malformed" if structure["status"] == "signature_range_mismatch" else "none"
            return self._finish(result, state)
        if not self.available():
            for s in sigs:
                result["signatures"].append({"byte_range": s["byte_range"], "integrity": "unverifiable", "reason": "validator_unavailable", "covers_whole_file": s["covers_whole_file"]})
            return self._finish(result, "not_validated")
        subfilters = [m.group(1).decode() for m in _SUBFILTER.finditer(data[:5_000_000])]
        for index, s in enumerate(sigs):
            result["signatures"].append(self._one(data, s, subfilters[index] if index < len(subfilters) else None))
        states = [x["state"] for x in result["signatures"]]
        worst = next((st for st in _SEVERITY if st in states), "not_validated")
        return self._finish(result, worst)

    def _one(self, data: bytes, sig: dict[str, Any], subfilter: str | None) -> dict[str, Any]:
        out: dict[str, Any] = {"byte_range": sig["byte_range"], "covers_whole_file": sig["covers_whole_file"], "subfilter": subfilter}
        der, signed, problem = _extract(data, sig["byte_range"])
        if problem:
            return {**out, "integrity": "malformed", "reason": problem, "state": "malformed", "trust": "not_trusted"}
        scratch = Path(tempfile.mkdtemp(prefix="channel360-sigcheck-", dir="/private/tmp" if __import__("sys").platform == "darwin" else None)).resolve()
        os.chmod(scratch, 0o700)
        try:
            (scratch / "sig.der").write_bytes(der)
            (scratch / "signed.bin").write_bytes(signed)
            base = ["cms", "-verify", "-inform", "DER", "-in", "sig.der", "-content", "signed.bin", "-binary", "-out", "/dev/null"]
            try:
                integrity = self._run(scratch, [*base, "-noverify", "-signer", "signer.pem"])
            except (OSError, SandboxFailed, SandboxUnavailable):
                return {**out, "integrity": "unverifiable", "reason": "validator_failed", "state": "not_validated", "trust": "not_established"}
            err = (integrity.stderr or "").lower()
            if integrity.returncode != 0:
                if "digest" in err or "signature failure" in err or "verify error" in err or "content" in err:
                    return {**out, "integrity": "invalid", "reason": "digest_or_signature_mismatch", "state": "integrity_invalid", "trust": "not_trusted"}
                return {**out, "integrity": "malformed", "reason": "signature_container_unreadable", "state": "malformed", "trust": "not_trusted"}
            out["integrity"] = "valid"
            out["signer"] = self._signer(scratch)
            out["timestamp_token_present"] = OID_TIMESTAMP in der
            out["signing_time_attribute_present"] = OID_SIGNING_TIME in der
            if self.trust_file is None:
                out.update(trust="not_established", trust_reason="no_trust_policy_configured")
                state = "integrity_valid_trust_not_established"
            else:
                chain = self._run(scratch, [*base, "-CAfile", str(self.trust_file), "-purpose", "any"])
                if chain.returncode == 0:
                    out.update(trust="trusted_by_policy"); state = "integrity_valid_trusted"
                else:
                    reason = "certificate_expired" if "expired" in (chain.stderr or "").lower() else "chain_not_trusted"
                    out.update(trust="not_trusted", trust_reason=reason); state = "integrity_valid_untrusted"
            out["state"] = "modified_after_signing" if not sig["covers_whole_file"] else state
            if not sig["covers_whole_file"]:
                out["integrity_of_signed_part"] = state
            return out
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _signer(self, scratch: Path) -> dict[str, str] | None:
        pem = scratch / "signer.pem"
        if not pem.is_file():
            return None
        try:
            proc = self._run(scratch, ["x509", "-in", "signer.pem", "-noout", "-subject", "-issuer", "-startdate", "-enddate"])
        except (OSError, SandboxFailed, SandboxUnavailable):
            return None
        info: dict[str, str] = {}
        for line in (proc.stdout or "").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                info[key.strip()] = value.strip()[:200]
        return {"subject": info.get("subject", ""), "issuer": info.get("issuer", ""), "not_before": info.get("notBefore", ""), "not_after": info.get("notAfter", "")} if info else None

    def _finish(self, result: dict[str, Any], state: str) -> dict[str, Any]:
        label, detail = LABELS[state]
        result.update(state=state, label=label, detail=detail, validated=state == "integrity_valid_trusted")
        return result
