from __future__ import annotations

import hashlib
import os
import socket
import stat
import struct
from collections.abc import Callable
from pathlib import Path

from packages.ingestion.models import ScanResult


class ClamAvUnixScanner:
    """ClamAV INSTREAM client over an explicitly configured local Unix socket."""

    def __init__(self, socket_path: str | Path, *, timeout_seconds: float = 10.0,
                 chunk_size: int = 64 * 1024,
                 socket_factory: Callable[..., socket.socket] | None = None):
        if timeout_seconds <= 0 or chunk_size < 1 or chunk_size > 1024 * 1024:
            raise ValueError("invalid_clamd_limits")
        self.socket_path = str(socket_path)
        self.timeout_seconds = timeout_seconds
        self.chunk_size = chunk_size
        self.socket_factory = socket_factory or (lambda *_: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))

    def scan(self, path: str | Path) -> ScanResult:
        digest = hashlib.sha256()
        source_path = Path(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(source_path, flags)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                os.close(descriptor)
                return ScanResult("error", hashlib.sha256(b"").hexdigest(), "clamd", reason_code="not_regular_file")
            with os.fdopen(descriptor, "rb", closefd=True) as source:
                with self.socket_factory(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(self.timeout_seconds)
                    client.connect(self.socket_path)
                    client.sendall(b"zINSTREAM\0")
                    while chunk := source.read(self.chunk_size):
                        digest.update(chunk)
                        client.sendall(struct.pack("!I", len(chunk)))
                        client.sendall(chunk)
                    client.sendall(struct.pack("!I", 0))
                    response = self._read_response(client)
        except (OSError, TimeoutError):
            return ScanResult("error", digest.hexdigest(), "clamd", reason_code="clamd_unavailable")

        if response == b"stream: OK":
            return ScanResult("clean", digest.hexdigest(), "clamd")
        if response.endswith(b" FOUND") and response.startswith(b"stream: "):
            return ScanResult("infected", digest.hexdigest(), "clamd", reason_code="malware_detected")
        return ScanResult("error", digest.hexdigest(), "clamd", reason_code="clamd_protocol_error")

    @staticmethod
    def _read_response(client: socket.socket) -> bytes:
        response = bytearray()
        while len(response) < 1024:
            chunk = client.recv(128)
            if not chunk:
                break
            response.extend(chunk)
            if b"\0" in response:
                break
        if not response.endswith(b"\0"):
            return b""
        return bytes(response[:-1])
