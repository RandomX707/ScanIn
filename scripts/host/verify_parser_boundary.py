#!/usr/bin/env python3
"""Host-only proof that parser code is sandboxed and cannot reach loopback TCP."""
from __future__ import annotations

import json
import select
import socket
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from packages.workers.isolation import RestrictedSubprocess
from packages.workers.parser import RestrictedParser


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: .venv/bin/python scripts/host/verify_parser_boundary.py WORKBOOK.xlsx", file=sys.stderr)
        return 2
    source = Path(sys.argv[1]).resolve(strict=True)
    report = RestrictedParser(PROJECT_ROOT).inspect(source)
    print(json.dumps({"status": report.status, "sha256": report.sha256, "capability": report.capability,
                      "adapter": report.adapter, "sheets": report.sheets}, sort_keys=True))

    accepted = threading.Event()
    stop = threading.Event()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.setblocking(False)
        host, port = listener.getsockname()

        def accept_probe() -> None:
            deadline = time.monotonic() + 8
            while not stop.is_set() and time.monotonic() < deadline:
                readable, _, _ = select.select([listener], [], [], 0.1)
                if readable:
                    try:
                        connection, _ = listener.accept()
                    except BlockingIOError:
                        continue
                    accepted.set()
                    connection.close()
                    return

        thread = threading.Thread(target=accept_probe, daemon=True)
        thread.start()
        code = (
            "import socket,sys; s=socket.socket(); "
            f"\ntry: s.connect(({host!r},{port})); print('NETWORK_ALLOWED'); sys.exit(10) "
            "\nexcept OSError: print('NETWORK_BLOCKED')"
        )
        result = RestrictedSubprocess(PROJECT_ROOT).run(
            [sys.executable, "-c", code], timeout_seconds=5
        )
        stop.set()
        thread.join(timeout=1)
        if accepted.is_set() or result.returncode != 0 or result.stdout.strip() != b"NETWORK_BLOCKED":
            print("FAIL: parser sandbox reached loopback TCP or the probe could not run", file=sys.stderr)
            return 1
    print("PASS: parser process could not reach an orchestrator loopback listener")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
