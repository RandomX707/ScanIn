from __future__ import annotations

import argparse
import base64
import json
import traceback
from pathlib import Path

from packages.core.models.contracts import ChangePermission, Patch, PatchPlan
from packages.core.verification.pdf import PdfIndependentVerifier
from packages.workers.parser import _value

MAX_JOB_BYTES = 150 * 1024 * 1024


def verify_job(path: Path) -> dict:
    raw_job = path.read_bytes()
    if len(raw_job) > MAX_JOB_BYTES:
        raise ValueError("pdf_verifier_input_invalid")
    job = json.loads(raw_job.decode("utf-8"))
    source = base64.b64decode(job["source"], validate=True)
    candidate = base64.b64decode(job["candidate"], validate=True)
    if len(source) > 50 * 1024 * 1024 or len(candidate) > 50 * 1024 * 1024:
        raise ValueError("pdf_verifier_input_invalid")
    raw = job["plan"]
    plan = PatchPlan(
        source_version_id=raw["source_version_id"], source_sha256=raw["source_sha256"],
        patches=tuple(Patch(item["source_version_id"], item["target_id"],
                             _value(item["expected_old_value"]), item["expected_fingerprint"],
                             _value(item["new_value"])) for item in raw["patches"]),
        permitted_changes=tuple(ChangePermission(**item) for item in raw["permitted_changes"]),
        provenance=tuple(tuple(item) for item in raw["provenance"]),
        dependents=tuple(raw["dependents"]), dependencies_unknown=bool(raw["dependencies_unknown"]),
        no_change=bool(raw["no_change"]), plan_sha256=raw["plan_sha256"],
    )
    return PdfIndependentVerifier().verify(source, candidate, plan).as_dict()


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    try:
        result = verify_job(Path(args.input))
        sysout = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
        print(sysout, end="")
        return 0
    except Exception as error:
        # Expose only a stable exception class/reason code, never document data.
        code = getattr(error, "reason_code", None) or getattr(error, "code", None)
        if not code:
            code = f"pdf_verifier_{type(error).__name__.lower()}"
        frames = [frame.name for frame in traceback.extract_tb(error.__traceback__)[-4:]]
        print(json.dumps({"error": str(code), "stage": frames}, separators=(",", ":")), end="")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
