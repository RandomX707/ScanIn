from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
import socket
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

# Allow direct invocation from the repository root or another working dir.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from packages.ingestion.pipeline import IngestionOrchestrator
from packages.ingestion.scanner import ClamAvUnixScanner
from packages.storage.local import ImmutableLocalStorage

EICAR_TEST_SIGNATURE = (
    b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
)
CLEAN_SYNTHETIC_CSV = b"claim_number,amount\nCLM-TEST-0001,10.00\n"
REJECTED_EXECUTABLE = b"MZ\x00synthetic rejected executable marker"


def main() -> int:
    parser = argparse.ArgumentParser(description="Exercise the configured ClamAV Unix socket using synthetic data and EICAR.")
    parser.add_argument("--socket", required=True, help="Configured clamd Unix socket path")
    parser.add_argument("--output", default="artifacts/host-integration/clamav-results.json")
    args = parser.parse_args()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(args.socket)
        client.sendall(b"zVERSION\0")
        version_data = bytearray()
        while len(version_data) < 1024:
            chunk = client.recv(128)
            if not chunk:
                break
            version_data.extend(chunk)
            if b"\0" in version_data:
                break
    if not version_data.endswith(b"\0"):
        raise RuntimeError("ClamAV did not return a complete version response")
    scanner_version = bytes(version_data[:-1]).decode("utf-8", errors="strict")
    report: dict[str, object] = {"scanner": "clamd", "scanner_version": scanner_version,
                                 "socket_configured": args.socket, "checks": []}
    with tempfile.TemporaryDirectory(prefix="idw-clamav-") as temporary:
        root = Path(temporary)
        storage = ImmutableLocalStorage(root / "objects")
        scanner = ClamAvUnixScanner(args.socket)
        pipeline = IngestionOrchestrator(storage, scanner)

        clean_bytes = CLEAN_SYNTHETIC_CSV
        clean_result = pipeline.process(uuid4(), uuid4(), uuid4(), BytesIO(clean_bytes),
                                        filename="synthetic-clean.csv")
        assert clean_result.outcome == "stored_private" and clean_result.stored_file is not None
        assert clean_result.scan is not None and clean_result.scan.status == "clean"
        assert clean_result.scan.sha256 == hashlib.sha256(clean_bytes).hexdigest()
        report["checks"].append({"check": "clean_synthetic_document", "status": "PASS",
                                 "scanner_status": clean_result.scan.status, "sha256": clean_result.scan.sha256,
                                 "promoted_to_original_storage": True})

        infected_result = pipeline.process(uuid4(), uuid4(), uuid4(), BytesIO(EICAR_TEST_SIGNATURE),
                                           filename="eicar-test.txt")
        assert infected_result.outcome == "quarantined" and infected_result.quarantine is not None
        assert infected_result.stored_file is None and infected_result.scan is not None
        assert infected_result.scan.status == "infected"
        report["checks"].append({"check": "eicar_test_signature", "status": "PASS",
                                 "scanner_status": infected_result.scan.status,
                                 "reason_code": infected_result.scan.reason_code,
                                 "quarantined": True, "promoted": False})

        rejected_result = pipeline.process(uuid4(), uuid4(), uuid4(), BytesIO(REJECTED_EXECUTABLE),
                                           filename="rejected.bin")
        assert rejected_result.outcome == "rejected" and rejected_result.stored_file is None
        report["checks"].append({"check": "rejected_executable", "status": "PASS",
                                 "reason_codes": list(rejected_result.detection.reasons), "promoted": False})

        unavailable_storage = ImmutableLocalStorage(root / "unavailable-objects")
        unavailable = IngestionOrchestrator(
            unavailable_storage, ClamAvUnixScanner(str(root / "missing-clamd.sock")))
        unscanned = unavailable.process(uuid4(), uuid4(), uuid4(), BytesIO(clean_bytes),
                                        filename="synthetic-unscanned.csv")
        assert unscanned.outcome == "scan_not_verified" and unscanned.stored_file is None
        assert unscanned.staged_path is not None and unscanned.staged_path.is_file()
        assert not list((root / "unavailable-objects").glob("**/originals/**"))
        report["checks"].append({"check": "scanner_unavailable_fail_closed", "status": "PASS",
                                 "pipeline_outcome": unscanned.outcome,
                                 "staged_private": True, "promoted": False})

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
