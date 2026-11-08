from __future__ import annotations

import hashlib
import socket
import struct
from pathlib import Path

from packages.ingestion.detect import Capability, detect_bytes
from packages.ingestion.scanner import ClamAvUnixScanner


def test_content_detection_uses_bytes_not_filename():
    result = detect_bytes(b"%PDF-1.7\n", filename="invoice.xlsx")
    assert result.media_type == "application/pdf"
    assert result.capability is Capability.READ_ONLY
    assert "adapter_not_implemented" in result.reasons


def test_openxml_container_is_not_promoted_without_sandboxed_inspection():
    result = detect_bytes(b"PK\x03\x04opaque", filename="contract.xlsx")
    assert result.media_type == "application/zip"
    assert result.capability is Capability.LIMITED
    assert "container_inspection_requires_sandbox" in result.reasons


def test_executables_and_oversized_inputs_are_rejected():
    executable = detect_bytes(b"MZ" + b"\0" * 20, filename="policy.pdf")
    oversized = detect_bytes(b"%PDF-" + b"x" * 10, filename="x.pdf", max_bytes=10)
    assert executable.capability is Capability.REJECTED
    assert "executable_content" in executable.reasons
    assert oversized.capability is Capability.REJECTED
    assert "file_too_large" in oversized.reasons


def test_unknown_and_unsupported_formats_are_explicitly_limited():
    unknown = detect_bytes(b"\x00\x01\x02", filename="anything")
    json_file = detect_bytes(b'{"policy": true}', filename="policy.json")
    assert unknown.capability is Capability.LIMITED
    assert "unknown_file_type" in unknown.reasons
    assert json_file.capability is Capability.READ_ONLY
    assert "adapter_not_implemented" in json_file.reasons


def test_png_image_signature_is_detected_without_decoding_payload():
    result = detect_bytes(b"\x89PNG\r\n\x1a\nopaque", filename="scan.bin")
    assert result.media_type == "image/png"
    assert result.sha256 == hashlib.sha256(b"\x89PNG\r\n\x1a\nopaque").hexdigest()


def test_clamd_instream_protocol_streams_content_and_classifies_verdict(tmp_path):
    payload = b"clam test data"

    class InMemoryClamdTransport:
        def __init__(self):
            self.sent = bytearray()
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def settimeout(self, _timeout): pass
        def connect(self, _path): pass
        def sendall(self, value): self.sent.extend(value)
        def recv(self, _count):
            response, self.response = getattr(self, "response", b"stream: OK\0"), b""
            return response

    transport = InMemoryClamdTransport()
    source = tmp_path / "staged.part"
    source.write_bytes(payload)
    result = ClamAvUnixScanner("unused.sock", chunk_size=4, socket_factory=lambda *_: transport).scan(source)

    assert bytes(transport.sent).startswith(b"zINSTREAM\0")
    framed = bytes(transport.sent)[len(b"zINSTREAM\0"):]
    decoded = bytearray()
    offset = 0
    while offset < len(framed):
        size = struct.unpack("!I", framed[offset:offset + 4])[0]
        offset += 4
        if size == 0:
            break
        decoded.extend(framed[offset:offset + size])
        offset += size
    assert bytes(decoded) == payload
    assert result.status == "clean"
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
